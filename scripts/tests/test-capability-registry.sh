#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Test suite for scripts/capability_registry.py — the machine-global capability
# registry (~/.bridge-capabilities/<type>.yaml). Mirrors the harness shape of
# scripts/tests/test-workspace-registry.sh (isolated registry dir per scenario,
# a real-dir untouched guard, a lock teeth check) but scoped to what this
# engine actually does: fail-open reads, upsert-by-(provider,registered_by)
# publish, registered_by-scoped remove, the closed entry schema, and
# fail-closed version/anomaly handling on write.
#
# HERMETIC: every scenario runs against an isolated $BRIDGE_CAPABILITIES_DIR
# (never the real ~/.bridge-capabilities) via the CLI's --registry-dir /
# BRIDGE_CAPABILITIES_DIR override — both suites pin it, as documented in
# docs/capability-registry.md.
#
# Run:  bash scripts/tests/test-capability-registry.sh
#       (exits non-zero on any failure).

set -u

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CR="$SCRIPTS_DIR/capability_registry.py"

PASS=0
FAIL=0
TMPS=""
cleanup() { for d in $TMPS; do rm -rf "$d"; done; }
trap cleanup EXIT

pass() { echo "  PASS — $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL — $1"; FAIL=$((FAIL + 1)); }

assert_contains() { case "$1" in *"$2"*) pass "$3";; *) fail "$3 (missing text: '$2')";; esac; }
assert_not_contains() { case "$1" in *"$2"*) fail "$3 (unexpected text: '$2')";; *) pass "$3";; esac; }
assert_eq() { if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (got: '$1', want: '$2')"; fi; }
assert_rc_zero() { if [ "$1" -eq 0 ]; then pass "$2"; else fail "$2 (rc=$1)"; fi; }
assert_rc_nonzero() { if [ "$1" -ne 0 ]; then pass "$2"; else fail "$2 (rc=0, expected failure)"; fi; }

# run <cmd...>  →  sets OUT (stdout+stderr) and RC
run() { OUT="$("$@" 2>&1)"; RC=$?; }

regdir() { local d; d=$(mktemp -d); TMPS="$TMPS $d"; echo "$d"; }

echo "════════════════════════════════════════════════════════════════"
echo "  capability-registry — test harness"
echo "════════════════════════════════════════════════════════════════"

[ -f "$CR" ] || { echo "  FAIL — $CR missing"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

REAL_DIR="${BRIDGE_CAPABILITIES_DIR:-$HOME/.bridge-capabilities}"
real_snapshot() { ( ls -laR "$REAL_DIR" 2>/dev/null; find "$REAL_DIR" -type f -exec shasum {} + 2>/dev/null ) | shasum | awk '{print $1}'; }
REAL_BEFORE="$(real_snapshot)"

# ---------------------------------------------------------------------------
echo ""
echo "-- 1. fail-open reads --"

d=$(regdir)
run python3 "$CR" --registry-dir "$d" list transcription
assert_rc_zero "$RC" "list on a missing dir/file exits 0 (fail-open)"
assert_contains "$OUT" "No entries registered" "missing type file reads as empty, not an error"

run python3 "$CR" --registry-dir "$d" list-types
assert_contains "$OUT" "No capability types registered" "empty registry dir lists no types"

run python3 "$CR" --registry-dir "$d" read transcription
assert_contains "$OUT" "entries: []" "read on a missing file returns an empty v1 envelope"

# ---------------------------------------------------------------------------
echo ""
echo "-- 2. publish — creates, then upserts by (provider, registered_by) --"

d=$(regdir)
run python3 "$CR" --registry-dir "$d" publish transcription \
    --provider meeting-transcription --registered-by instance-a \
    --host worker-host --launchd-label com.openbridge.transcribe-worker \
    --contexts-dir '~/transcribe-pipeline/contexts'
assert_rc_zero "$RC" "first publish exits 0"
[ -f "$d/transcription.yaml" ] && pass "publish creates <type>.yaml" || fail "publish creates <type>.yaml"

run python3 "$CR" --registry-dir "$d" list transcription
n=$(echo "$OUT" | grep -c "instance-a")
assert_eq "$n" "1" "exactly one entry for instance-a after first publish"

first_at=$(python3 "$CR" --registry-dir "$d" read transcription | grep -A1 "registered_by: instance-a" | grep registered_at | head -1)
sleep 1.1
run python3 "$CR" --registry-dir "$d" publish transcription \
    --provider meeting-transcription --registered-by instance-a --host worker-host-2
assert_rc_zero "$RC" "re-publish (same provider+instance) exits 0"
run python3 "$CR" --registry-dir "$d" list transcription
n=$(echo "$OUT" | grep -c "instance-a")
assert_eq "$n" "1" "re-publish UPDATES in place — still exactly one instance-a row"
assert_contains "$OUT" "worker-host-2" "re-publish refreshes the fields (new host)"
second_at=$(python3 "$CR" --registry-dir "$d" read transcription | grep -A1 "registered_by: instance-a" | grep registered_at | head -1)
if [ "$first_at" != "$second_at" ]; then pass "re-publish refreshes registered_at"; else fail "re-publish refreshes registered_at (unchanged: $first_at)"; fi

# ---------------------------------------------------------------------------
echo ""
echo "-- 3. multi-instance — same provider, different registered_by, no collision --"

run python3 "$CR" --registry-dir "$d" publish transcription \
    --provider meeting-transcription --registered-by instance-b --host worker-b
assert_rc_zero "$RC" "instance-b publishes the SAME provider without error"
run python3 "$CR" --registry-dir "$d" list transcription
n=$(echo "$OUT" | grep -cE "instance-a|instance-b")
assert_eq "$n" "2" "two distinct instances' entries for the same provider both survive"

# ---------------------------------------------------------------------------
echo ""
echo "-- 4. remove — scoped to registered_by, never touches another instance --"

run python3 "$CR" --registry-dir "$d" remove transcription --registered-by instance-a
assert_rc_zero "$RC" "remove exits 0"
assert_contains "$OUT" "removed 1 entr" "remove reports exactly 1 entry removed"
run python3 "$CR" --registry-dir "$d" list transcription
assert_not_contains "$OUT" "instance-a" "instance-a's entry is gone after its own remove"
assert_contains "$OUT" "instance-b" "instance-b's entry is UNTOUCHED by instance-a's remove"

run python3 "$CR" --registry-dir "$d" remove transcription --registered-by instance-a
assert_rc_zero "$RC" "remove with nothing left to remove still exits 0 (safe no-op)"
assert_contains "$OUT" "removed 0 entr" "second remove reports 0 — not an error"

run python3 "$CR" --registry-dir "$d" remove transcription --registered-by instance-b --provider some-other-provider
assert_contains "$OUT" "removed 0 entr" "remove scoped to a non-matching provider removes nothing"
run python3 "$CR" --registry-dir "$d" list transcription
assert_contains "$OUT" "instance-b" "instance-b's real entry survives a provider-mismatched remove"

# ---------------------------------------------------------------------------
echo ""
echo "-- 5. closed entry schema — mechanically enforced, not just documented --"

run python3 -c "
import sys; sys.path.insert(0, '$SCRIPTS_DIR')
import capability_registry as cr
try:
    cr._validate_entry({'provider': 'x', 'registered_by': 'y', 'registered_at': 'now',
                         'customer_name': 'ACME Corp'})
    print('FAIL: unknown field accepted')
except cr.RegistrySchemaError as e:
    print('OK:', e)
"
assert_contains "$OUT" "OK:" "_validate_entry rejects a field outside the closed allowlist"
assert_contains "$OUT" "customer_name" "the rejection names the offending field"

run python3 -c "
import sys; sys.path.insert(0, '$SCRIPTS_DIR')
import capability_registry as cr
r = cr.Registry('transcription', '$d')
try:
    r.publish('p', 'instance-z', customer_name='ACME')
    print('FAIL: publish() accepted an unlisted kwarg')
except TypeError as e:
    print('OK: publish() has no parameter to smuggle an unlisted field through:', e)
"
assert_contains "$OUT" "OK:" "publish()'s fixed signature structurally can't accept an unknown field"

# ---------------------------------------------------------------------------
echo ""
echo "-- 6. fail-closed on write anomalies --"

d=$(regdir)
mkdir -p "$d"
printf 'not: [valid, yaml' > "$d/transcription.yaml"
before=$(shasum "$d/transcription.yaml")
run python3 "$CR" --registry-dir "$d" publish transcription --provider p --registered-by i
assert_rc_nonzero "$RC" "publish onto a corrupt file refuses (fail-closed)"
after=$(shasum "$d/transcription.yaml")
assert_eq "$before" "$after" "corrupt file bytes are left untouched, not rotated or clobbered"

d=$(regdir)
mkdir -p "$d"
cat > "$d/transcription.yaml" <<'YAML'
version: 999
entries: []
YAML
run python3 "$CR" --registry-dir "$d" read transcription
assert_rc_zero "$RC" "reading a newer-version file is still ALLOWED"
run python3 "$CR" --registry-dir "$d" publish transcription --provider p --registered-by i
assert_rc_nonzero "$RC" "publishing onto a newer-version file REFUSES the write"

d=$(regdir)
mkdir -p "$d"
printf 'entries: []\n' > "$d/transcription.yaml"
run python3 "$CR" --registry-dir "$d" publish transcription --provider p --registered-by i
assert_rc_nonzero "$RC" "a missing/non-numeric version refuses the write"

# ---------------------------------------------------------------------------
echo ""
echo "-- 7. lock — parallel publishes from distinct instances all survive --"

d=$(regdir)
pids=""
for i in $(seq 1 8); do
  python3 "$CR" --registry-dir "$d" publish transcription --provider p --registered-by "inst-$i" >/dev/null 2>&1 &
  pids="$pids $!"
done
for p in $pids; do wait "$p"; done
run python3 "$CR" --registry-dir "$d" list transcription
n=$(echo "$OUT" | grep -c "^p ")
assert_eq "$n" "8" "8 parallel publishes from 8 instances → all 8 rows survive (flock holds)"

# ---------------------------------------------------------------------------
echo ""
echo "-- 8. real ~/.bridge-capabilities/ untouched across the entire run --"

REAL_AFTER="$(real_snapshot)"
if [ "$REAL_BEFORE" = "$REAL_AFTER" ]; then
  pass "the real ~/.bridge-capabilities/ snapshot is byte-identical before/after"
else
  fail "the real ~/.bridge-capabilities/ snapshot CHANGED — a test leaked outside its isolated dir"
fi

# ---------------------------------------------------------------------------
echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
