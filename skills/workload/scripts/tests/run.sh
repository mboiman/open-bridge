#!/bin/bash
# Regression suite for skills/workload, in the shape of
# workflow/workloads/_tests/run.sh: one line per case, then the tally.
#
#   ./scripts/tests/run.sh                 everything
#   ./scripts/tests/run.sh render          only cases whose name matches
#   WORKLOAD_UPDATE_GOLDEN=1 ./run.sh      record the reviewed byte goldens
#
# The suite starts real processes in exactly one place, test_exec.py, and they
# are local, harmless and die under their own deadline. It never touches ssh,
# launchctl, systemctl or crontab: a guard in tests/conftest.py raises if any
# test tries, because real services run on the machines these fixtures describe.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL="$(cd "$DIR/../.." && pwd)"
cd "$SKILL" || exit 2

PATTERN="${1:-}"

if [ "$PATTERN" = "--mutate" ]; then
  # The proof over the proof. Each entry in tests/mutations.py softens ONE
  # literal in an engine source and names the ONE test that must go red for it.
  # A mutation that stays green means the suite does not examine that behaviour,
  # whatever its name promises.
  #
  # Every mutation is applied to a SCRATCH COPY, never to the working tree, and
  # the copy is thrown away afterwards.
  exec python3 "$DIR/mutate.py"
fi

RAW="$(mktemp -t workload-tests)"
trap 'rm -f "$RAW"' EXIT

if [ -n "$PATTERN" ]; then
  python3 -m unittest discover -v -s tests -t . -k "$PATTERN" >"$RAW" 2>&1
else
  python3 -m unittest discover -v -s tests -t . >"$RAW" 2>&1
fi

awk -f "$DIR/tally.awk" "$RAW"
status=$?

if [ "$status" -ne 0 ]; then
  # A red verdict is not always a traceback: "everything was skipped" and
  # "nothing was collected" both fail with no failure block to show.
  detail="$(sed -n '/^======/,$p' "$RAW" | head -60)"
  if [ -n "$detail" ]; then
    echo
    echo "first failures in detail:"
    printf '%s\n' "$detail"
  fi
fi

exit "$status"
