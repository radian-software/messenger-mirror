#!/usr/bin/env python3

import abc
import base64
import datetime
import logging
import os
import pathlib
import re
import time
import sys
import urllib.parse

import chromedriver_py
import dotenv
import persistqueue
import requests
from selenium.common.exceptions import NoSuchElementException
import selenium.webdriver
import sendgrid
import sendgrid.helpers.mail as sendgrid_mail


logging.basicConfig(level=logging.INFO)
dotenv.load_dotenv()

FACEBOOK_EMAIL = os.environ["FACEBOOK_EMAIL"]
FACEBOOK_PASSWORD = os.environ["FACEBOOK_PASSWORD"]
FACEBOOK_USER_ID = os.environ["FACEBOOK_USER_ID"]
MM_DEBUG = bool(os.environ.get("MM_DEBUG", ""))
MM_NOTIFICATION_FREQUENCY = int(os.environ.get("MM_NOTIFICATION_FREQUENCY", "3600"))
SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]
SENDGRID_FROM_ADDRESS = os.environ["SENDGRID_FROM_ADDRESS"]
SENDGRID_TO_ADDRESS = os.environ["SENDGRID_TO_ADDRESS"]

sendgrid_client = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY).client


QUEUE_FILE = pathlib.Path(__file__).parent / "notifications_queue"


class State(abc.ABC):
    @abc.abstractmethod
    def detect(self, driver):
        pass

    @abc.abstractmethod
    def action(self, driver):
        pass


class StateUnknown(State):
    def detect(self, driver):
        # Always return true, this state will be at the end of the
        # list and will match if nothing else does, hence "unknown".
        return True

    def action(self, driver):
        if MM_DEBUG:
            breakpoint()
        else:
            driver.get(f"https://messenger.com")


class StateInitial(State):
    def detect(self, driver, **kw):
        return not driver.title

    def action(self, driver, **kw):
        driver.get(f"https://messenger.com")


class StateEmailPasswordPage(State):
    def detect(self, driver, **kw):
        try:
            self.email_input = driver.find_element_by_id("email")
            self.password_input = driver.find_element_by_id("pass")
            self.remember_me_checkbox = driver.find_element_by_name("persistent")
            self.login_button = driver.find_element_by_id("loginbutton")
        except NoSuchElementException:
            return False
        else:
            return True

    def action(self, driver, **kw):
        self.email_input.send_keys(FACEBOOK_EMAIL)
        self.password_input.send_keys(FACEBOOK_PASSWORD)
        if not self.remember_me_checkbox.is_selected():
            driver.execute_script("arguments[0].click()", self.remember_me_checkbox)
        self.login_button.click()


class StateConversationList(State):
    def detect(self, driver, **kw):
        try:
            self.chats_list = driver.find_element_by_css_selector(
                "[aria-label='Chats']"
            )
        except NoSuchElementException:
            return False
        else:
            return True


class StateViewingConversation(StateConversationList):
    def detect(self, driver, **kw):
        if not super().detect(driver):
            return False
        return not driver.current_url.endswith(f"/{FACEBOOK_USER_ID}")

    def action(self, driver, **kw):
        driver.get(f"https://www.messenger.com/t/{FACEBOOK_USER_ID}")


class StateGotMessage(StateConversationList):
    def detect(self, driver, **kw):
        if not super().detect(driver):
            return False
        try:
            self.mark_as_read_button = self.chats_list.find_element_by_css_selector(
                "[aria-label='Mark as Read']"
            )
        except NoSuchElementException:
            return False
        else:
            return True

    def action(self, driver, queue, **kw):
        thread_container = self.mark_as_read_button.find_element_by_xpath(
            "ancestor::*[@data-testid='mwthreadlist-item']"
        )
        spans = [
            span.text for span in thread_container.find_elements_by_tag_name("span")
        ]
        conversation_name = spans[0]
        message = spans[spans.index("") - 1]
        photo_url = thread_container.find_element_by_css_selector(
            "svg image"
        ).get_property("href")["baseVal"]
        conversation_id = re.search(
            r"/([0-9]+)/?$",
            thread_container.find_element_by_tag_name("a").get_property("href"),
        ).group(0)
        resp = requests.get(photo_url)
        resp.raise_for_status()
        photo_b64 = str(base64.b64encode(resp.content))
        notification = {
            "id": conversation_id,
            "name": conversation_name,
            "message": message,
            "url": f"https://www.messenger.com/t/{conversation_id}/",
            "photo_b64": photo_b64,
        }
        logging.info(
            f"Queuing notification (id={conversation_id}, name={repr(conversation_name)})"
        )
        queue.put(notification)
        self.mark_as_read_button.click()


class StateWaitingForMessages(StateConversationList):
    def detect(self, driver, **kw):
        return super().detect(driver)

    def action(self, driver, **kw):
        pass


class Mirror:

    ALL_STATES = [
        StateInitial(),
        StateEmailPasswordPage(),
        StateViewingConversation(),
        StateGotMessage(),
        StateWaitingForMessages(),
        StateUnknown(),
    ]

    def __init__(self):
        self.driver = selenium.webdriver.Chrome(
            executable_path=chromedriver_py.binary_path
        )
        self.queue = persistqueue.Queue(QUEUE_FILE)

    def run(self):
        last_update = datetime.datetime.fromtimestamp(0)
        while True:
            for state in Mirror.ALL_STATES:
                if state.detect(driver=self.driver):
                    self.state = state
                    break
            logging.info(f"State: {self.state.__class__.__name__}")
            self.state.action(driver=self.driver, queue=self.queue)
            if (
                now := datetime.datetime.now()
            ) - last_update > MM_NOTIFICATION_FREQUENCY:
                notifications = []
                while not self.queue.empty():
                    notifications.append(self.queue.get_nowait())
                grouped_notifications = {}
                for notification in notifications:
                    cid = notification["id"]
                    grouped_notifications[cid] = notification
                notifications = list(grouped_notifications.values())
                if notifications:
                    logging.info(f"Sending {len(notifications)} notification(s)")
                    sendgrid_client.mail.send.post(
                        request_body=sendgrid_mail.Mail(
                            sendgrid_mail.Email(
                                email=SENDGRID_FROM_ADDRESS, name="Veidt"
                            ),
                            sendgrid_mail.To(SENDGRID_TO_ADDRESS),
                            "[Messenger] Message(s) from "
                            + ", ".join(nf["name"] for nf in notifications),
                            sendgrid_mail.Content(
                                "text/plain",
                                self.format_email(
                                    "\n\n".join(
                                        f"[{nf['name']}] @ {nf['url']}\n{nf['message']}"
                                        for nf in notifications
                                    )
                                ),
                            ),
                        ).get()
                    )
                self.queue.task_done()
            time.sleep(5)


def main():
    mirror = Mirror()
    mirror.run()


if __name__ == "__main__":
    main()
    sys.exit(0)
