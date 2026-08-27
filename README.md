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

## Fixture data

`football_data.py` is a small client for the football-data.org v4 API
(free key from https://www.football-data.org/client/register, passed via
`FOOTBALL_DATA_API_KEY`), with on-disk caching under `.cache/`.

`fetch_matches.py` lists fixtures for a given matchday:

```bash
export FOOTBALL_DATA_API_KEY=your-key-here
python3 fetch_matches.py --matchday 2
```

There's no prediction model here yet — an earlier attempt (a Poisson
over/under-2.5-goals model) was backtested and found statistically
indistinguishable from chance on one season of data, so it was pulled
rather than kept as something that looked more validated than it was.
See `docs/football-data-api-reference.md` for the underlying API.
