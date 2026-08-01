#!/bin/bash
# review.sh — board-pilot `review` stage (scope: core).
#
# The reviewer is a MODEL. Its verdict is an opinion about work of the same model
# family, and the dossier labels it exactly that. This stage's job is to make the
# opinion attributable: it decides against criteria/review.md, whose path+SHA the
# engine records, so "the reviewer rejected for the wrong reason" becomes a file to
# diff instead of a mystery to argue about.
#
# THE EXIT-CODE CONTRACT, identical to verify.sh and for the same reason: a reject is
# a VERDICT, so this script exits 0. The engine reads the sidecar only when
# result.ok, so exiting non-zero on reject would silently discard the verdict AND
# route rework onto the uncapped on_fail edge. Non-zero here means the REVIEW ITSELF
# failed to happen (no parseable verdict) -> on_fail -> park.
#
# The SCRIPT writes the sidecar, not the model: the model emits JSON on stdout and
# this script validates it before it becomes a verdict. A model writing $VERDICT_FILE
# directly could emit a malformed or hostile sidecar, and an unparseable one would
# read as "no verdict" — byte-identical to "the reviewer had nothing to say".
#
# Env from the runner: ITEM_ID ITEM_TITLE ITEM_URL ITEM_BODY_FILE BRANCH EVIDENCE_DIR
# CRITERIA_FILE VERDICT_FILE BOUNCES. cwd = the target repo clone.
#
# ITEM_BODY_FILE does NOT add a second standard — criteria/review.md stays the only
# one. It supplies the EVIDENCE that standard already demands: its "the diff contains
# work nobody asked for" rule is unjudgeable against a title alone, because a title
# cannot say what was asked. The requirement can.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

: "${ITEM_ID:?}" "${ITEM_TITLE:?}" "${BRANCH:?}" "${EVIDENCE_DIR:?}" "${CRITERIA_FILE:?}" "${VERDICT_FILE:?}"

bp_refuse_protected_branch
bp_resolve_claude
[ -r "$CRITERIA_FILE" ] || bp_die "criteria file unreadable: $CRITERIA_FILE"

bp_ensure_branch
BASE="$(bp_base_branch)"

PROMPT="Review the change on the current branch of THIS repository against a written standard.

The standard is in the file ${CRITERIA_FILE}. Read it FIRST. You are applying THAT
standard and nothing else — not your general taste. If the standard does not cover
something, that is not grounds to reject; it is grounds to say so in your annotation.

$(bp_story_block)

The requirement is NOT a second standard: you still judge only against ${CRITERIA_FILE}.
It is the evidence that standard needs — it tells you what was actually ASKED FOR, so
you can tell a change that meets the need from one carrying work nobody requested. A
change that departs from the requirement because the CODE required it is correct, not a
defect, provided it says so.

Inspect the real change with your read tools. The diff is 'origin/${BASE}...HEAD'; read
the changed files themselves, not only the diff.

Output STRICT JSON on stdout and nothing else — no prose, no code fence, no preamble:
{\"verdict\": \"pass\" | \"reject\", \"annotation\": \"<your reason, specific and short>\"}

'reject' sends the item back for rework and consumes one round of a hard budget; the
annotation is the ONLY thing the implementer will see, so name the concrete defect and
the file it is in. 'pass' means it meets the standard, not that it is perfect.

$(bp_untrusted_block)"

RAW_FILE="$(mktemp)"
trap 'rm -f "$RAW_FILE"' EXIT

# Command substitution, not a pipe: a crashed reviewer must fail the stage, not be
# reported as an empty verdict.
RAW="$(bp_claude "Read,Glob,Grep" "$PROMPT")"
printf '%s' "$RAW" > "$RAW_FILE"

# Validate before it becomes a verdict. No parseable verdict -> non-zero -> park:
# a reviewer that cannot state a verdict has not reviewed, and defaulting such a run
# to 'pass' would let a broken reviewer wave everything through to a PR.
python3 - "$RAW_FILE" "$VERDICT_FILE" <<'PY'
import json, re, sys

raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
obj = None
try:
    obj = json.loads(raw.strip())
except json.JSONDecodeError:
    # A model asked for bare JSON still tends to wrap it. Take the LAST balanced
    # object: a preamble may mention a shape, the answer comes last.
    for m in reversed(list(re.finditer(r"\{.*\}", raw, re.S))):
        try:
            obj = json.loads(m.group(0))
            break
        except json.JSONDecodeError:
            continue
if not isinstance(obj, dict):
    sys.exit("[board-pilot] FAIL: reviewer emitted no parseable JSON verdict")

verdict = obj.get("verdict")
if verdict not in ("pass", "reject"):
    sys.exit("[board-pilot] FAIL: reviewer verdict is %r, expected 'pass' or 'reject'" % (verdict,))
annotation = obj.get("annotation")
annotation = annotation if isinstance(annotation, str) else ""
if verdict == "reject" and not annotation.strip():
    # An empty note burns a rework round and tells the implementer nothing. The
    # engine already parks on a bounced item with no note; refuse to author one.
    sys.exit("[board-pilot] FAIL: reviewer rejected with an empty annotation")

with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump({"verdict": verdict, "annotation": annotation}, f)
PY

# A copy for the dossier: $VERDICT_FILE lives in a per-run temp dir the pr stage
# never sees. This is a CLAIM, stored to be quoted under an [agent] label — not
# evidence, and the dossier says so.
mkdir -p "$EVIDENCE_DIR/review"
cp "$VERDICT_FILE" "$EVIDENCE_DIR/review/verdict.json"

echo "[review] OK: verdict recorded for ${ITEM_ID} against $(basename "$CRITERIA_FILE")"
