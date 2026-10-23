#!/bin/bash
# Restore the gitignored API-Football and model caches from the project's
# shared folder, and install Python deps. Runs at session start (see
# .claude/settings.json). The caches are kept as ONE packed file because
# copying ~35k small JSON files off the shared mount takes ~45 minutes;
# the packed file restores in seconds.
#
# After a run that fetched new data, save the caches back with:
#   scripts/restore_caches.sh save
set -e
ARCHIVE=/mnt/project-files/cache/caches.tar.gz
cd "$(dirname "$0")/.."

if [ "$1" = "save" ]; then
    tar czf /tmp/caches.tar.gz .cache_apifootball .cache_model
    cp /tmp/caches.tar.gz "$ARCHIVE.tmp" && mv "$ARCHIVE.tmp" "$ARCHIVE"
    rm /tmp/caches.tar.gz
    echo "saved caches to $ARCHIVE"
    exit 0
fi

python3 -c "import dill, sklearn, statsmodels" 2>/dev/null \
    || pip install -q --only-binary=:all: -r requirements.txt
if [ -f "$ARCHIVE" ] && [ ! -d .cache_apifootball ]; then
    tar xzf "$ARCHIVE"
    echo "restored caches from $ARCHIVE"
fi
