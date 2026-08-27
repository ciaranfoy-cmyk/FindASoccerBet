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

See `docs/football-data-api-reference.md` for the underlying API.

## Dataset + statistical analysis

An earlier prediction model (Poisson-based) was backtested and found
statistically indistinguishable from chance on one season of data, so it
was pulled. This is the proper follow-up: a real per-match dataset with
point-in-time features (no lookahead), tested proportionately —
multiple-comparison-corrected significance, and an honest chronological
train/test split rather than in-sample fit. Full writeup:
`docs/dataset-analysis.md`.

Short version: a few features (recent scoring form, top-flight
experience, which league) are genuinely, statistically correlated with
total goals — but none of them, alone or combined, predict an individual
match's over/under-2.5 outcome better than chance out-of-sample (AUC
~0.50). Real signal, not enough of it to be useful for prediction from
box-score data alone.

```bash
export FOOTBALL_DATA_API_KEY=your-key-here
pip install -r requirements.txt

python3 build_dataset.py     # writes data/matches.csv (2,700+ matches)
python3 analyze_dataset.py   # correlation table + model results
```
