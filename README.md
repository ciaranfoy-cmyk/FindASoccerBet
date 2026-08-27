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

It also reports an over/under-2.5-goals read — a separate model that
ignores home/away and just compares each team's overall attacking/
defensive rate, since that's what backtesting validated (see
`docs/over25-model-validation.md`). Rather than forcing a fixed number of
picks each week, it lists whichever fixtures clear `--over25-threshold`:
a full-season backtest of 2025-26 showed the model's real edge over
typical bookmaker odds only shows up from ~65% confidence upward — maybe
1-2 games a week, not every week — so a forced weekly pick count would
have diluted the very thing worth using it for.

Promoted/relegated teams get their scoring record pulled from the
Championship too (this API's only other English league), so they're not
simply excluded for lack of Premier League history. A team with no
top-flight games in the window gets a defensive penalty applied on top —
backtesting the three teams promoted for 2025-26 found their Premier
League defense was ~0.6 goals/game leakier than their Championship-
blended record predicted; their attack undershot it too, by ~0.3
goals/game.

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

# Only flag over-2.5 picks at 65%+ confidence (where backtesting showed
# a real edge over ~1.60 odds), instead of the 60% default
python3 analyze_matchweek.py --over25-threshold 0.65

# Skip pulling in the other English league's data for promoted/relegated teams
python3 analyze_matchweek.py --no-cross-division
```

Standings for completed seasons are cached on disk under `.cache/` (they
never change, so they're fetched once); fixture lists are always fetched
fresh.

`fetch_matches.py` remains available for a plain fixture listing without
the analysis:

```bash
python3 fetch_matches.py --matchday 2
```
