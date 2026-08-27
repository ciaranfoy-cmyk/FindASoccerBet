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

## Weekly matchweek analysis

`analyze_matchweek.py` ranks a Premier League matchweek's fixtures by how
likely each is to finish 0-0, using a Poisson expected-goals model built
from each team's home/away scoring and conceding record over the last 3
completed seasons, blended with each team's last few games of form (a
multi-season baseline alone can miss a real in-season trend). It also
flags fixtures where a historically prolific home-scoring side hosts a
team that's both bottom-of-the-table and leaky defensively over that same
window.

Requires a free API key from https://www.football-data.org/client/register,
passed via `FOOTBALL_DATA_API_KEY`.

```bash
export FOOTBALL_DATA_API_KEY=your-key-here

# Analyze the next unplayed matchweek (blends in recent form; takes a
# couple of minutes due to the free tier's 10 requests/minute limit)
python3 analyze_matchweek.py

# Analyze a specific matchday
python3 analyze_matchweek.py --matchday 5

# Use a different 3-season (or custom) historical window
python3 analyze_matchweek.py --seasons 2022 2023 2024

# Skip the recent-form blend — season history only, much faster
python3 analyze_matchweek.py --no-form

# Tune the recent-form blend
python3 analyze_matchweek.py --form-games 6 --form-weight 0.4
```

Standings for completed seasons are cached on disk under `.cache/` (they
never change, so they're fetched once); fixture lists are always fetched
fresh.

`fetch_matches.py` remains available for a plain fixture listing without
the analysis:

```bash
python3 fetch_matches.py --matchday 2
```
