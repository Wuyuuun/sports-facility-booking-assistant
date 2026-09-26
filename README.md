# Sports Facility Booking Assistant

A desktop tool that books a public badminton court on the Macau government sports-facility
booking service on my behalf, at the moment the court is released for booking.

Written for my own use: it books one court, for me, on one venue.

## What it automates, and what it deliberately does not

The interesting part of this project is the line I drew between the two.

**Automated:** OAuth sign-in, session and browser lifecycle, reading the venue and court list
from the booking API, waiting for the release time and submitting the booking the instant the
slot becomes available, extracting the resulting order number, and reading the 6-digit SMS
verification code from my own Mac's Messages database.

**Deliberately left to a human:** the slider captcha, and the payment.

When the portal demands a slider captcha, the tool stops, brings the browser to the front and
waits for me to complete it — then picks the flow back up by watching for the booking request
on the wire rather than trying to defeat the check. Payment is never touched:
the SMS code is entered, the order is confirmed, and I pay.

Both of those were design choices, not missing features. A booking tool that quietly solves
captchas and moves money is a different thing from one that removes the tedium of sitting
at a keyboard at 07:30 and typing.

## How it works

- **Browser control over CDP.** Drives a real Chrome session via the DevTools Protocol, so
  the login is a genuine login and the site sees a normal browser.
- **Request recorder.** Captures API traffic and responses, which is what makes the booking
  step reliable: the order number lives in the response body of the booking call.
- **Release-time scheduler.** Aligns to the 07:30 release window and fires the booking call
  when the slot appears.
- **SMS reader.** Reads the verification code from the local Messages database (SQLite),
  filtered by the sender number of the sports bureau so it cannot pick up an unrelated code.
- **GUI and state machine.** A local HTML/JS interface for selecting dates and courts and for
  watching what the bot is doing, with state persisted between runs.

## Engineering notes

The bug I remember is a race condition that cost me several failed bookings. The script
treated the booking call as failed whenever it could not parse the response — but the response
body was sometimes not yet captured when the request itself was observed. So the order had
actually been created, the script reported failure, and it skipped the whole verification and
payment path. The fix was to make the recorder wait for the response body, with a timeout,
before parsing anything. That kind of bug is invisible in a unit test and obvious the first
time it costs you a booking.

## Files

| Path | What it is |
|---|---|
| `main.py` | Entry point |
| `courtbot/login.py` | OAuth sign-in flow |
| `courtbot/booking_api.py` | Booking API calls |
| `courtbot/runner.py` | Main flow and state machine |
| `courtbot/captcha.py` | Captcha hand-off to the user |
| `courtbot/sms.py` | Verification-code retrieval |
| `courtbot/browser.py` | CDP browser control |
| `courtbot_gui.py`, `courtbot_gui.html` | Local GUI |
| `config.yaml` | Venue, court and timing configuration |
| `README.zh.md` | The original Chinese notes for this project |

## Running it

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in your own portal credentials
.venv/bin/python courtbot_gui.py
```

Credentials are read from the environment; `.env`, browser profiles, captured traffic and
runtime state are all excluded from version control.

## Tools

Python, Chrome DevTools Protocol, Selenium, SQLite, YAML configuration, HTML/JavaScript.
