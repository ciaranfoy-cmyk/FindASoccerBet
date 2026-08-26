#!/usr/bin/env python3
"""Send a 'Hello World' email over SMTP.

Configuration is read entirely from environment variables so no
credentials are hardcoded:

    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       e.g. 587
    SMTP_USERNAME   the account used to authenticate/send
    SMTP_PASSWORD   an app password (for Gmail: myaccount.google.com/apppasswords)
    FROM_EMAIL      defaults to SMTP_USERNAME if unset
    TO_EMAIL        defaults to ciaranfoy@gmail.com

Usage:
    python3 send_hello_email.py
"""

import os
import smtplib
import sys
from email.message import EmailMessage

DEFAULT_TO_EMAIL = "ciaranfoy@gmail.com"


def main() -> int:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    from_email = os.environ.get("FROM_EMAIL", username)
    to_email = os.environ.get("TO_EMAIL", DEFAULT_TO_EMAIL)

    missing = [
        name
        for name, value in (
            ("SMTP_HOST", host),
            ("SMTP_USERNAME", username),
            ("SMTP_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        print("See send_hello_email.py docstring for setup instructions.", file=sys.stderr)
        return 1

    message = EmailMessage()
    message["Subject"] = "Hello World"
    message["From"] = from_email
    message["To"] = to_email
    message.set_content("Hello World")

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(message)

    print(f"Sent 'Hello World' email to {to_email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
