#!/usr/bin/env bash
# Run onerc_healthcare tests on the dedicated test site, under the DB lock.
# Usage:
#   scripts/run_tests.sh                    # migrate, then the whole app
#   scripts/run_tests.sh test_install       # migrate, then onerc_healthcare.tests.test_install
#   scripts/run_tests.sh --no-migrate test_install
#   scripts/run_tests.sh onerc_healthcare.emr_visit.doctype.patient_visit.test_patient_visit
set -u
SITE="${SITE:-emr-test.localhost}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MIGRATE=1
if [ "${1:-}" = "--no-migrate" ]; then MIGRATE=0; shift; fi
MODULE="${1:-}"
case "$MODULE" in
  "") ARGS="--app onerc_healthcare" ;;
  onerc_healthcare.*) ARGS="--app onerc_healthcare --module $MODULE" ;;
  *) ARGS="--app onerc_healthcare --module onerc_healthcare.tests.$MODULE" ;;
esac
CMD="bench --site $SITE run-tests $ARGS"
if [ "$MIGRATE" = 1 ]; then CMD="bench --site $SITE migrate && $CMD"; fi
exec "$HERE/db_lock.sh" "$CMD"
