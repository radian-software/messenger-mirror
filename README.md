# Messenger Mirror

This repository has a small Python application which will allow you to
receive an email notification when somebody sends you a Facebook
message.

## Why?

I don't like Facebook as a company, and I don't want to support them
by using their products, including Messenger. However, when I came
around to this point of view, I already had a number of existing
contacts on Messenger. I migrated everyone I talked to regularly onto
other platforms, but in case someone messaged me out of the blue, I
still wanted to know about that, so I could redirect them onto Signal
or SMS.

With the help of this application, I can make sure I won't miss it
when a very old contact happens to message me on Facebook, while not
having to ever actively check Messenger or keep it on my phone.

## How?

Facebook aggressively discourages reverse-engineering the Messenger
API by handing out suspensions and bans like candy whenever they
detect anything remotely anomalous going on. Because of this, the most
practical way to spoof Messenger is to just run the whole dang thing
inside Selenium, which is more or less indistinguishable from human
usage.

The assumption is you have a private server where you can run this
application 24/7. It automatically logs into Messenger using your
credentials, waits for messages to come in, batches up the
notifications in a persistent queue, and sends them to your email via
SendGrid.

## Setup

You'll want to sign up for a [SendGrid](https://sendgrid.com/) account
(free) and get your API key, as well as set up a verified sender
address. This works best when you own a custom domain (not free; I use
[Namecheap](https://www.namecheap.com/) for my domains); if you try to
send from a Gmail address or similar with SendGrid, your emails have a
high probability of tripping spam filters because it can be detected
that they didn't actually come from Gmail servers. Of course, if
setting up a custom domain, you probably want replies to be
receivable, so I would suggest [Forward
Email](https://forwardemail.net/) (free) for an easy way to get your
MX records in order.

Then you want to create your `.env` file in the repository toplevel
directory, as follows:

```
FACEBOOK_EMAIL=your.email@example.com
FACEBOOK_PASSWORD=correct horse battery staple
FACEBOOK_USER_ID=100006953043135
MM_DEBUG=1
MM_HEADLESS=0
MM_NOTIFICATION_FREQUENCY=60
SENDGRID_API_KEY=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
SENDGRID_FROM_ADDRESS=your.verified.email@example.com
SENDGRID_TO_ADDRESS=your.email@example.com
```

Fill in `FACEBOOK_EMAIL` and `FACEBOOK_PASSWORD` with your email and
password, naturally. Get `FACEBOOK_USER_ID` by going to Messenger,
going to your chat with yourself, and looking in the URL. Set
`MM_NOTIFICATION_FREQUENCY` to the maximum number of seconds you want
Messenger Mirror to wait before sending you the notifications it's
received (this acts as a debouncing factor to avoid too many emails).
The `SENDGRID_` variables come from your SendGrid console.

Now you want to install [Poetry](https://python-poetry.org/) and run
`poetry install` and `poetry shell`, then start the server with
`python3 -m messenger_mirror`. You'll have to have a matching version
of Chrome installed per `chromedriver-py` in the `pyproject.toml`. If
not, update `pyproject.toml` to match the version reported in the
error message, and run `poetry lock` and `poetry install` again.

You should see Selenium open up a Chrome window and navigate to
Messenger, login automatically, and start reading your messages. At
this stage you should iron out any bugs you see.

Next step is to get things running on your remote server. This is a
bit more complicated on account of Facebook being really suspicious of
EC2 and other cloud provider IP addresses.

I suggest SSH'ing into your server with X forwarding enabled (`ssh
-X`; you may have to set `X11Forwarding yes` in
`/etc/ssh/sshd_config`). Then you can run as above, and step through
the added verification steps. I found that selecting email code
verification was the most robust technique. Once I logged in once
successfully, my server's IP address appeared to be allowlisted.

After you've got things working in debug mode, set `MM_DEBUG=0` in
your `.env` file, bump up to `MM_NOTIFICATION_FREQUENCY=3600`, and run
the server *ad infinitum*. Here's an example `start.sh`:

```
#!/usr/bin/env bash

set -e
set -o pipefail

if [[ -f "$HOME/.profile" ]]; then
    . "$HOME/.profile"
fi

cd "$HOME/path/to/messenger-mirror"
poetry install
poetry run python3 -m messenger_mirror
```

And example systemd unit file:

```
[Unit]
Description=Messenger Mirror
After=network-online.target

[Service]
Type=exec
ExecStart=/home/yourname/path/to/start.sh
User=yourname
Restart=always

[Install]
WantedBy=multi-user.target
```

In the case that the server hits a page it doesn't know how to parse,
it'll save a screenshot in the `screenshots` directory in the repo,
log the error, and attempt a retry. Two fails within 60 seconds will
cause it to abort, in order to avoid tripping anti-bot detection. The
server also hosts a simple Flask API locally on port 4209:

* `POST localhost:4209/screenshot/foobar`: will save a screenshot of
  the current browser window to `screenshots/foobar.png`

OK, now you've got the basic part set up, there is monitoring to think
about. Ideally you want to be alerted when the app crashes, because
then you'll stop getting your notifications. What I've set up is a
Messenger bot that will repeatedly message me every few hours, but
then the notifications from this specific are emailed to a separate
email. Then, we can be guaranteed that Messenger Mirror should receive
a couple messages a day, every day. So all we have to do is then set
up [Dead Man's Snitch](https://deadmanssnitch.com/) on a separate
email endpoint to make sure we keep hitting that notification codepath
every day, and I'll get an email if things are bricked.

Unfortunately, Facebook is the worst thing ever, so setting this up is
a real Jenga tower. You want to start by creating a Facebook app at
[Facebook for Developers](https://developers.facebook.com/), enabling
the Messenger product, clicking through some privacy surveys, filling
in terms of service URLs etc., creating a Facebook page through the
developer interface, setting a public username so you can search for
it later, attaching the page to the app, and generating a page token.

That's the easy part.

So next you want to go to [this Glitch project I set
up](https://glitch.com/edit/#!/messenger-mirror-psid-extractor) (yes,
really, Facebook recommends using Glitch for this) and fork it. You'll
get a new URL of the form `https://something.glitch.me/webhook` for
your app. You want to go back to Facebook for Developers, enable
webhooks for your app, fill in the callback URL to point at Glitch,
generate a verification token and fill it in there, go back to Glitch,
fill in the `PAGE_ACCESS_TOKEN` and `VERIFY_TOKEN` appropriately (you
have to do this and restart the app *before* you can exit the webhook
modal on Facebook), go to Messenger, search for your page using the
username you configured earlier, send it a message, and check the logs
on Glitch where you should finally see a user ID printed.

All that effort was necessary to find the "page-specific user ID" (or
PSID) which is needed in order to use the Messenger API to send
messages to yourself. I wish I were joking. Anyway, set that as
`FACEBOOK_USER_PSID` in `.env`.

You can then provision a free Dead Man's Snitch account by creating an
empty [Heroku](https://heroku.com/) app and adding it as a plugin.
Yeah, I don't know why or how they allow that, but it works, even if
you don't use Heroku for anything else. What we want to do is use DMS
in email mode: set `SENDGRID_TO_ADDRESS_FOR_PINGS` to the email
address for your snitch. (Note that this requires your Messenger bot
is called `Messenger Mirror`.) Set `MM_PING_FREQUENCY=28800` in `.env`
(or substitute how many seconds you want between automated messages
sent by the Messenger bot), fill in `FACEBOOK_PAGE_TOKEN`, and you are
off to the races.

## Limitations

* Does not send you message bodies or profile pictures even though
  this information is gathered. Just tells you what conversation(s)
  have updates, and links you to them. Optimized for the case of
  people migrating off Messenger.
* May run into SendGrid free tier limits for large message volumes.
  Again, optimized for the case of people migrating off Messenger.
* Screws with message unread status (messages are marked as read as
  soon as they are processed and the notification is queued, even
  before any email is sent), so people may think you have seen their
  message before you have actually have.
* Not guaranteed to be reliable. Race conditions exist where an
  incoming message will never result in a notification, although I
  expect these to be fairly rare.
* Assumes you have a server where you can run this application 24/7
  ish. This doesn't have to have any uptime guarantees; a home
  workstation would work just fine, or you could hypothetically even
  do it on a laptop, if you can sacrifice the CPU and memory needed to
  run an instance of Chrome open in the background.
