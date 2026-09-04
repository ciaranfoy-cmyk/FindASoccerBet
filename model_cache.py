"""On-disk cache for the expensive, purely-deterministic pieces of a
prediction run: the trained models, the live confidence bars, and the
replayed historical team state. All three depend only on the contents
of data/*.csv -- same inputs, same outputs, always -- so caching them
turns a multi-minute retrain-from-scratch into a sub-second load for
repeated prediction requests against the same data snapshot. A cache
entry is keyed by a fingerprint of every data/*.csv's mtime + size, so
it is invalidated automatically the instant any of that data changes
(e.g. after pulling in new match results) -- a cache hit can never be
stale relative to what a fresh run would produce.

Uses dill, not stdlib pickle: the replayed team-history state (built by
predict_upcoming.new_state()) is a dict of defaultdicts with lambda
default_factories, which stdlib pickle cannot serialize.
"""

import glob
import hashlib
import os

import dill

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache_model")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def data_fingerprint() -> str:
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    parts = [f"{os.path.basename(f)}:{os.path.getmtime(f)}:{os.path.getsize(f)}" for f in files]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def load(key: str):
    path = os.path.join(CACHE_DIR, f"{key}_{data_fingerprint()}.dill")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return dill.load(f)


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
