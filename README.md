# FindASoccerBet

## Hello World email script

`send_hello_email.py` sends a "Hello World" email over SMTP. It uses only
the Python standard library, so no dependencies need to be installed.

### Setup

1. Copy `.env.example` to `.env` and fill in your SMTP credentials.
   - For Gmail: enable 2-Step Verification, then create an App Password at
     https://myaccount.google.com/apppasswords and use that as
     `SMTP_PASSWORD` (your normal Gmail password will not work).
2. Load the env vars and run the script:

   ```bash
   export $(grep -v '^#' .env | xargs)
   python3 send_hello_email.py
   ```

By default the email is sent to `ciaranfoy@gmail.com`; override with the
`TO_EMAIL` environment variable.
