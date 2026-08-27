#!/bin/bash
# Regression suite for the `intentionally_absent` block in
# infra/remotes/_schema.yaml.
#
#   run.sh            every valid/ file must pass, every invalid/ file must
#                     fail AND name the reason it was written for. A control
#                     that fails for the wrong reason proves nothing.
#   run.sh --mutate   soften ONE rule in a scratch copy of the schema and prove
#                     that exactly the controls belonging to that rule go
#                     hollow. A control that stays red while its own rule is
#                     gone was never testing that rule.
#
# WHY THIS BLOCK HAS A SUITE AT ALL. The parent object is
# `additionalProperties: true`, because remotes are heterogeneous and v1 is
# deliberately permissive. That permissiveness is also why an undefended field
# is worse here than elsewhere: a misspelled or half written block is accepted
# in silence and then reads, to a human skimming the file, exactly like a
# complete one. Every control below is a shape a live inventory either already
# contained or would plausibly grow.
#
# WHAT THE BATTERY DOES NOT PROVE, said here rather than left to be discovered:
# `since` carries a `pattern` AND a `format: date`, on purpose. Under a
# validator that checks formats they overlap, so removing the pattern alone
# leaves every control red. The pattern is not therefore hollow; it is the half
# that bites where `format` is annotation only, which is the draft 2020-12
# default. The battery states this instead of arranging a fixture that would
# pretend otherwise.
#
# The `_`-prefix keeps this folder out of `infra/remotes/*.yaml`, which is what
# validate-bridge.py globs, so these deliberately invalid files never reach it.

set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 2

SCHEMA="infra/remotes/_schema.yaml"
DIR="infra/remotes/_tests"
FAILED=0

if ! command -v check-jsonschema >/dev/null 2>&1; then
    echo "check-jsonschema not on PATH (pipx install check-jsonschema), skipping"
    exit 0
fi

# file | needle its error must contain | sed removing the rule | controls that
# rule owns (space separated; more than one means the rule is shared)
CONTROLS=(
  "absence-as-bare-string.yaml|is not of type 'object'|s/^          type: object\$/          type: [object, string]/|absence-as-bare-string.yaml"
  "absence-without-since.yaml|'since' is a required property|s/required: \[since, reason\]/required: [reason]/|absence-without-since.yaml"
  "absence-without-reason.yaml|'reason' is a required property|s/required: \[since, reason\]/required: [since]/|absence-without-reason.yaml"
  "absence-non-iso-date.yaml|does not match|/pattern: \"\\^\\[0-9\\]{4}/d;/format: date/d|absence-non-iso-date.yaml absence-impossible-date.yaml"
  "absence-impossible-date.yaml|is not a 'date'|/format: date/d|absence-impossible-date.yaml"
  "absence-typo-key.yaml|Additional properties are not allowed|s/additionalProperties: false/additionalProperties: true/|absence-typo-key.yaml"
)

field() { echo "$1" | cut -d'|' -f"$2"; }

check() {  # schema, file -> 0 when the file validates
    check-jsonschema --schemafile "$1" "$2" >/dev/null 2>&1
}

# `pipefail` turns a matched grep into a failed pipeline whenever the producer
# exits non-zero, and check-jsonschema always does on an invalid file. So the
# output is captured first and matched afterwards.
reason_matches() {  # schema, file, needle
    local out
    out="$(check-jsonschema --schemafile "$1" "$2" 2>&1)"
    case "$out" in (*"$3"*) return 0 ;; (*) return 1 ;; esac
}

# ---------------------------------------------------------------- the suite --
if [ "${1:-}" != "--mutate" ]; then
    echo "== valid cases (must pass)"
    for f in "$DIR"/valid/*.yaml; do
        if check "$SCHEMA" "$f"; then
            printf "  ok    %s\n" "$(basename "$f")"
        else
            printf "  FAIL  %s  (a legitimate shape is refused)\n" "$(basename "$f")"
            check-jsonschema --schemafile "$SCHEMA" "$f" 2>&1 | sed -n '2,4p' | sed 's/^/        /'
            FAILED=1
        fi
    done

    echo "== negative controls (must fail, for their own reason)"
    for entry in "${CONTROLS[@]}"; do
        name="$(field "$entry" 1)"; needle="$(field "$entry" 2)"
        f="$DIR/invalid/$name"
        if check "$SCHEMA" "$f"; then
            printf "  FAIL  %-32s accepted; the rule is not enforced\n" "$name"
            FAILED=1
        elif reason_matches "$SCHEMA" "$f" "$needle"; then
            printf "  ok    %-32s refused for \"%s\"\n" "$name" "$needle"
        else
            printf "  FAIL  %-32s refused, but NOT for \"%s\"\n" "$name" "$needle"
            FAILED=1
        fi
    done

    # Nothing in invalid/ may be left out of the table above: a fixture nobody
    # runs is a fixture nobody proved.
    for f in "$DIR"/invalid/*.yaml; do
        b="$(basename "$f")"
        printf '%s\n' "${CONTROLS[@]}" | cut -d'|' -f1 | grep -qx "$b" \
            || { printf "  FAIL  %-32s lies in invalid/ but no control claims it\n" "$b"; FAILED=1; }
    done

    [ "$FAILED" -eq 0 ] && echo "suite green" || echo "suite RED"
    exit "$FAILED"
fi

# ------------------------------------------------------------ the mutations --
echo "== mutation battery: each rule removed in turn"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

for entry in "${CONTROLS[@]}"; do
    rule="$(field "$entry" 1)"; mutation="$(field "$entry" 3)"; owned="$(field "$entry" 4)"
    cp "$SCHEMA" "$SCRATCH/schema.yaml"
    sed -i '' -e "$mutation" "$SCRATCH/schema.yaml" 2>/dev/null \
        || sed -i -e "$mutation" "$SCRATCH/schema.yaml"

    if ! diff -q "$SCHEMA" "$SCRATCH/schema.yaml" >/dev/null; then
        hollow=""
        for f in "$DIR"/invalid/*.yaml; do
            check "$SCRATCH/schema.yaml" "$f" && hollow="$hollow $(basename "$f")"
        done
        hollow="$(echo "$hollow" | tr ' ' '\n' | sort | xargs)"
        expect="$(echo "$owned" | tr ' ' '\n' | sort | xargs)"
        if [ "$hollow" = "$expect" ]; then
            printf "  ok    rule of %-32s gone -> hollow: %s\n" "$rule" "$hollow"
        else
            printf "  FAIL  rule of %-32s gone -> expected [%s], got [%s]\n" "$rule" "$expect" "${hollow:-nothing}"
            FAILED=1
        fi
    else
        printf "  FAIL  the mutation for %s changed nothing in the schema\n" "$rule"
        FAILED=1
    fi
done

# A softened schema must still accept what was always legitimate, otherwise the
# battery would be measuring a broken schema rather than a missing rule.
for f in "$DIR"/valid/*.yaml; do
    check "$SCHEMA" "$f" || { echo "  FAIL  valid case broke: $(basename "$f")"; FAILED=1; }
done

[ "$FAILED" -eq 0 ] && echo "battery green: every control proved" || echo "battery RED"
exit "$FAILED"
