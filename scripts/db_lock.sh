#!/usr/bin/env bash
# Serialise all DB work on the bench (parallel agents must not interleave migrations/tests).
# Usage: scripts/db_lock.sh "<command run from the bench directory>"
#   scripts/db_lock.sh "bench --site emr-test.localhost migrate"
set -u
BENCH_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
LOCK="$BENCH_DIR/.emr-db.lock"
TIMEOUT=1800
waited=0
until mkdir "$LOCK" 2>/dev/null; do
  sleep 3; waited=$((waited+3))
  if [ "$waited" -gt "$TIMEOUT" ]; then
    echo "db_lock: waited >30min; remove $LOCK if stale" >&2
    exit 2
  fi
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM
cd "$BENCH_DIR"
bash -c "$1"
