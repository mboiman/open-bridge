#!/bin/sh
# Guard script for the workload 'block-style-report', rendered by the workload skill.
#
# Do not edit. It is rendered from the workload's own file, and an edit
# here is reported as drift on the next pass, never taken as an
# improvement. It supplies what the backend underneath does not promise:
#   deadline
#   missing_detection
#   process_group_kill
set -u

# A service manager gives no login PATH. Naming it is the difference
# between a run and a line in a log nobody reads.
#
# The package manager prefixes come FIRST, and that is the half this
# line was missing. `/usr/bin:/bin:/usr/sbin:/sbin` reaches the base
# system and almost nothing an operator installs: measured on a real
# machine, `gh` did not resolve at all under it and `python3` resolved
# to the system one, which carries no third party module. A job moved
# onto this guard would have swapped a working report for an empty
# one, exit code zero and no error anywhere.
#
# Both prefixes, and no platform branch: /opt/homebrew/bin is Apple
# Silicon, /usr/local/bin is Intel and most Linux boxes, and a
# directory that does not exist costs a lookup and nothing else.
# Ahead of /usr/bin rather than behind it, because behind it changes
# nothing for a name the base system also ships, which is the case
# that actually matters.
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH

BRIDGE_WORKLOAD=block-style-report
BRIDGE_WORKLOAD_DIGEST=sha256:e942bc789c69b6d301471bba3ebd7a14532763b747cd5c679807175b8306a89c
export BRIDGE_WORKLOAD BRIDGE_WORKLOAD_DIGEST

STATE_DIR=/home/opuser/.bridge/workloads
mkdir -p "$STATE_DIR" || exit 71
DEADLINE_SEC=600
EXPIRED_FLAG="$STATE_DIR/block-style-report.expired"
rm -f "$EXPIRED_FLAG"
TRACE_FILE="$STATE_DIR/block-style-report.trace"
OUT_FILE="$STATE_DIR/block-style-report.out"
OUT_CAP_BYTES=262144

# One line per run: this is what makes an absent run detectable at
# all. Without it, nothing distinguishes a run that failed from a
# run that never started.
BRIDGE_WORKLOAD_TRACE_ID=block-style-report
trace() {
    printf '%s workload=%s rc=%s duration_sec=%s verdict=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BRIDGE_WORKLOAD_TRACE_ID" \
        "$1" "$2" "$3" >> "$TRACE_FILE"
}

started=$(date +%s)
# The run needs a process group of its own, or the watchdog can
# only end the direct child and a grandchild keeps the output
# pipe open. `setsid` puts it in one deterministically, and it
# is reached for FIRST because the shell route is not reliable:
# dash refuses job control without a controlling terminal, which
# is exactly how cron starts things, and there /bin/sh IS dash.
# Started without job control on purpose, so the background job
# is not already a group leader and setsid does not fork away
# from the pid this script waits on.
if command -v setsid >/dev/null 2>&1; then
    setsid /opt/bridge/scripts/daily-health-report.sh > "$OUT_FILE" 2>&1 &
else
    # No setsid (a stock macOS has none). Monitor mode is the
    # fallback where the shell can arrange it; the watchdog reads
    # the group back and decides from what it actually finds.
    set -m 2>/dev/null || :
    /opt/bridge/scripts/daily-health-report.sh > "$OUT_FILE" 2>&1 &
    set +m 2>/dev/null || :
fi
CHILD=$!
# No timeout(1): a stock macOS does not ship it. A watchdog that
# counts in whole seconds needs nothing but sh, ps and sleep.
(
    waited=0
    while [ "$waited" -lt "$DEADLINE_SEC" ]; do
        kill -0 "$CHILD" 2>/dev/null || exit 0
        sleep 1
        waited=$((waited + 1))
    done
    kill -0 "$CHILD" 2>/dev/null || exit 0
    : > "$EXPIRED_FLAG"
    run_group=$(ps -o pgid= -p "$CHILD" 2>/dev/null | tr -d " ")
    own_group=$(ps -o pgid= -p $$ 2>/dev/null | tr -d " ")
    if [ -n "$run_group" ] && [ "$run_group" != "$own_group" ]; then
        # The run has a group to itself: end it whole. A
        # grandchild holding the output pipe dies with it, which
        # is the difference between a cleanup and a hang.
        kill -TERM -$run_group 2>/dev/null
        sleep 2
        kill -KILL -$run_group 2>/dev/null
    elif [ "$own_group" = "$$" ]; then
        # One group for this whole run, and it belongs to nobody
        # else: a service manager starts each job that way. The
        # guard goes down with it, so the record is written first.
        trace 143 "$DEADLINE_SEC" expired
        printf '%s reached its deadline of %ss and was ended\n' \
            "$BRIDGE_WORKLOAD" "$DEADLINE_SEC" >&2
        kill -TERM -$own_group 2>/dev/null
        sleep 2
        kill -KILL -$own_group 2>/dev/null
    else
        # Shared group, and not ours to end: killing it would
        # take the caller down too. Only the run itself is ended,
        # and the shortfall is said out loud rather than hidden.
        printf '%s: no process group of its own, only the run itself was ended\n' \
            "$BRIDGE_WORKLOAD" >&2
        kill -TERM "$CHILD" 2>/dev/null
        sleep 2
        kill -KILL "$CHILD" 2>/dev/null
    fi
) &
WATCHDOG=$!
# stderr of `wait` is dropped because a shell with job control
# announces a signalled job there, and that announcement carries the
# whole watchdog into the service log on every deadline.
wait "$CHILD" 2>/dev/null
rc=$?
kill "$WATCHDOG" 2>/dev/null
wait "$WATCHDOG" 2>/dev/null
if [ -s "$OUT_FILE" ]; then
    tail -c "$OUT_CAP_BYTES" "$OUT_FILE" > "$OUT_FILE.cap" 2>/dev/null \
        && mv -f "$OUT_FILE.cap" "$OUT_FILE" \
        || rm -f "$OUT_FILE.cap"
fi

finished=$(date +%s)
duration=$((finished - started))

if [ -f "$EXPIRED_FLAG" ]; then
    rm -f "$EXPIRED_FLAG"
    # An expired deadline is a reported error, never silence.
    printf '%s reached its deadline of %ss and was ended\n' \
        "$BRIDGE_WORKLOAD" "$DEADLINE_SEC" >&2
    trace "$rc" "$duration" expired
    exit "$rc"
fi
if [ "$rc" -eq 0 ]; then
    trace "$rc" "$duration" ok
else
    trace "$rc" "$duration" failed
fi
exit "$rc"
