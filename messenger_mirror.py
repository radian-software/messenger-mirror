#!/usr/bin/env python3

import abc
import base64
import datetime
import json
import logging
import os
import pathlib
import re
import threading
import time
import sys
import urllib.parse

import chromedriver_py
import dotenv
import flask
import persistqueue
import requests
from selenium.common.exceptions import NoSuchElementException
import selenium.webdriver
import sendgrid
import sendgrid.helpers.mail as sendgrid_mail


def parse_bool(val):
    val = val.lower()
    if (
        val.startswith("y")
        or val.startswith("t")
        or val.startswith("1")
        or val.startswith("on")
    ):
        return True
    if (
        val.startswith("n")
        or val.startswith("f")
        or val.startswith("0")
        or val.startswith("off")
    ):
        return False
    raise ValueError(f"can't interpret boolean {val}")


logging.basicConfig(level=logging.INFO)
dotenv.load_dotenv()

FACEBOOK_EMAIL = os.environ["FACEBOOK_EMAIL"]
FACEBOOK_PASSWORD = os.environ["FACEBOOK_PASSWORD"]
FACEBOOK_USER_ID = os.environ["FACEBOOK_USER_ID"]
FACEBOOK_USER_PSID = os.environ.get("FACEBOOK_USER_PSID") or None
FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN") or None
MM_DEBUG = parse_bool(os.environ.get("MM_DEBUG") or "0")
MM_HEADLESS = parse_bool(os.environ.get("MM_HEADLESS") or "0")
MM_NOTIFICATION_FREQUENCY = int(os.environ.get("MM_NOTIFICATION_FREQUENCY") or "3600")
MM_PING_FREQUENCY = int(os.environ.get("MM_PING_FREQUENCY") or "28800")
SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]
SENDGRID_FROM_ADDRESS = os.environ["SENDGRID_FROM_ADDRESS"]
SENDGRID_TO_ADDRESS = os.environ["SENDGRID_TO_ADDRESS"]
SENDGRID_TO_ADDRESS_FOR_PINGS = os.environ.get("SENDGRID_TO_ADDRESS") or None

sendgrid_client = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY).client


QUEUE_FILE = pathlib.Path(__file__).parent / "notifications_queue"


def save_screenshot(driver, name):
    screenshots_dir = pathlib.Path(__file__).parent / "screenshots"
    screenshots_dir.mkdir(exist_ok=True)
    screenshot_file = screenshots_dir / f"{name}.png"
    driver.save_screenshot(str(screenshot_file))


class State(abc.ABC):
    @abc.abstractmethod
    def detect(self, driver, **kw):
        pass

    @abc.abstractmethod
    def action(self, driver, **kw):
        pass


class StateUnknown(State):
    def __init__(self):
        self.last_failure = None

    def detect(self, driver, **kw):
        # Always return true, this state will be at the end of the
        # list and will match if nothing else does, hence "unknown".
        return True

    def action(self, driver, **kw):
        if MM_DEBUG:
            breakpoint()
        else:
            ts = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            save_screenshot(driver, f"unknown_{ts}")
            if (
                self.last_failure
                and (datetime.datetime.now() - self.last_failure).total_seconds()
                < 60 * 5
            ):
                logging.error("Got into an unknown state twice in 5 minutes, exiting")
                sys.exit(1)
            logging.warning("Got into an unknown state, restarting from the beginning")
            driver.get(f"https://messenger.com")
            self.last_failure = datetime.datetime.now()


class StateInitial(State):
    def detect(self, driver, **kw):
        return driver.title in {"", "New Tab"}

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
        driver.execute_script("arguments[0].click()", self.login_button)


class StateConversationList(State):
    def detect(self, driver, **kw):
        try:
            self.chats_list = driver.find_element_by_css_selector(
                "[aria-label='Chats']"
            )
        except NoSuchElementException:
            try:
                if "No messages found." not in [
                    span.text
                    for span in driver.find_elements_by_css_selector(
                        "div[id] div[data-testid='MWJewelThreadListContainer'] span[dir='auto']"
                    )
                ]:
                    return False
            except NoSuchElementException:
                return False
            else:
                self.chats_list = None
                return True
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
        if not super().detect(driver) or not self.chats_list:
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
        ).group(1)
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
        time.sleep(60)


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
        options = selenium.webdriver.ChromeOptions()
        if MM_HEADLESS or not MM_DEBUG:
            options.add_argument("--headless")
        options.add_argument("--user-data-dir=user")
        options.add_argument("--window-size=3840,2160")
        self.driver = selenium.webdriver.Chrome(
            executable_path=chromedriver_py.binary_path,
            options=options,
        )
        self.queue = persistqueue.Queue(QUEUE_FILE)

    def start_server(self):
        app = flask.Flask(__name__)

        @app.route("/screenshot/<name>", methods=["POST"])
        def screenshot(name):
            save_screenshot(self.driver, name)
            return f"screenshot saved under {name}.png"

        threading.Thread(target=lambda: app.run(port=4209), daemon=True).start()

    def send_pings_foreground(self):
        while True:
            time.sleep(MM_PING_FREQUENCY)
            logging.info(f"Sending ping to PSID {FACEBOOK_USER_PSID}")
            resp = requests.post(
                "https://graph.facebook.com/v2.6/me/messages",
                params={
                    "access_token": FACEBOOK_PAGE_TOKEN,
                    "recipient": json.dumps({"id": FACEBOOK_USER_PSID}),
                    "message": json.dumps({"text": "Hello from Messenger Mirror"}),
                    "messaging_type": "MESSAGE_TAG",
                    "tag": "CONFIRMED_EVENT_UPDATE",
                },
            )
            resp.raise_for_status()

    def send_pings(self):
        if FACEBOOK_USER_PSID and FACEBOOK_PAGE_TOKEN:
            logging.info(
                f"Messenger bot pings are enabled with PSID {FACEBOOK_USER_PSID}"
            )
            threading.Thread(
                target=lambda: self.send_pings_foreground(), daemon=True
            ).start()

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
                (now := datetime.datetime.now()) - last_update
            ).total_seconds() > MM_NOTIFICATION_FREQUENCY:
                last_update = now
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
                    for nf in notifications:
                        sendgrid_client.mail.send.post(
                            request_body=sendgrid_mail.Mail(
                                sendgrid_mail.Email(
                                    email=SENDGRID_FROM_ADDRESS, name="Messenger"
                                ),
                                sendgrid_mail.To(
                                    (
                                        SENDGRID_TO_ADDRESS_FOR_PINGS
                                        if nf["name"] == "Messenger Mirror"
                                        else None
                                    )
                                    or SENDGRID_TO_ADDRESS
                                ),
                                f"Message(s) from {nf['name']}",
                                sendgrid_mail.Content(
                                    "text/plain",
                                    nf["url"],
                                ),
                            ).get()
                        )
                    self.queue.task_done()
            time.sleep(1)


def main():
    mirror = Mirror()
    mirror.start_server()
    mirror.send_pings()
    mirror.run()


if __name__ == "__main__":
    main()
    sys.exit(0)
