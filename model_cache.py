"""On-disk cache for the expensive, purely-deterministic pieces of a
prediction run: the trained models, the live confidence bars, and the
replayed historical team state. All three depend only on the contents
of the model's TRAINING data (data/*.csv), not on every file that
happens to live in data/ -- kalshi_trades.csv, forward_test_log.csv and
similar logs live there too and change on every trade/settle, which
would silently invalidate this cache (forcing a full retrain) on data
that has nothing to do with what the model was trained on. A cache
entry is keyed by a fingerprint of only TRAINING_FILES' mtime + size,
so it is invalidated automatically the instant training data actually
changes (e.g. after pulling in new match results) -- a cache hit can
never be stale relative to what a fresh run would produce -- while
being immune to unrelated writes elsewhere in data/.

Uses dill, not stdlib pickle: the replayed team-history state (built by
predict_upcoming.new_state()) is a dict of defaultdicts with lambda
default_factories, which stdlib pickle cannot serialize.
"""

import hashlib
import os

import dill

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache_model")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Explicit allowlist, not a glob over data/*.csv -- see module docstring.
# Every file the live model's training pipeline actually reads from disk.
TRAINING_FILES = [
    "matches_apifootball.csv",
    "lineup_features.csv",
    "player_form_features.csv",
    "shots_venue_features.csv",
    "xg_features.csv",
    "xg_weighted_features.csv",
    "calibrators.pkl",
]


def data_fingerprint() -> str:
    parts = []
    for name in sorted(TRAINING_FILES):
        path = os.path.join(DATA_DIR, name)
        if os.path.exists(path):
            parts.append(f"{name}:{os.path.getmtime(path)}:{os.path.getsize(path)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def load(key: str):
    """Returns None on any load failure, not just a missing file -- the
    fingerprint only tracks data/*.csv, not the calling code, so a code
    change that alters what gets cached (e.g. the bundle's tuple shape)
    can leave a stale, incompatible file sitting under a still-valid
    fingerprint. Treating that as a cache miss (retrain) is always safe;
    letting it raise is not.
    """
    path = os.path.join(CACHE_DIR, f"{key}_{data_fingerprint()}.dill")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return dill.load(f)
    except Exception:
        return None


def save(key: str, obj) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = data_fingerprint()
    # Drop stale entries for this key (from a previous fingerprint) so the
    # cache directory doesn't grow unbounded as the underlying data changes.
    for stale in glob.glob(os.path.join(CACHE_DIR, f"{key}_*.dill")):
        if not stale.endswith(f"{fp}.dill"):
            os.remove(stale)
    with open(os.path.join(CACHE_DIR, f"{key}_{fp}.dill"), "wb") as f:
        dill.dump(obj, f)
