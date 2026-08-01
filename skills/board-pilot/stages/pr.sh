#!/bin/bash
# pr.sh — board-pilot `pr` stage (scope: core). The last stage; `gate: human` stops here.
#
# The draft PR is the file a human merges on, so this stage's product is a DOSSIER:
# every claim carries who made it and how much it is worth. The labels are not
# decoration — they are the deliverable. Softening one is the change that makes this
# pipeline lie, which is why a test pins each of them literally.
#
# What this stage does NOT do, and why:
#   * NO issue comment. The old script posted one: a second comment writer with a
#     different format, a different idempotency mechanism, argv instead of stdin, an
#     emoji badge CLAUDE.md forbids — and unguarded under `set -e`, so its 403 parked
#     the item AFTER the PR was already open. The record has ONE writer: the engine.
#   * NO `--body "$BODY"` on argv. The body carries agent-authored text of unbounded
#     length; it rides stdin via --body-file -.
#   * NO citing its own CI. CI starts after the PR opens and this stage is the last
#     one before the human gate, so the body links the Checks tab and says so.
#
# unset GH_TOKEN: the App installation has issues:write + organization_projects:write
# but NO pull_requests and NO contents (verified live), so `gh pr create` cannot run
# under it. Dropping it falls back to ambient gh — bot-written record, human-written PR.
#
# Env from the runner: ITEM_ID ITEM_TITLE ITEM_URL BRANCH EVIDENCE_DIR BOUNCES.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

: "${ITEM_ID:?}" "${ITEM_TITLE:?}" "${BRANCH:?}" "${EVIDENCE_DIR:?}"

bp_refuse_protected_branch
unset GH_TOKEN

GH="${BP_GH:-gh}"
command -v "$GH" >/dev/null 2>&1 || bp_die "gh not found (BP_GH='${BP_GH:-}')"

BASE="$(bp_base_branch)"

# Issue number from the trigger URL; empty for a draft card. `require_issue: true`
# means the engine never arms one, so this is a belt to that suspenders.
ISSUE_NUM="$(printf '%s' "${ITEM_URL:-}" | grep -oE 'issues/[0-9]+' | grep -oE '[0-9]+' || true)"

BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

# ---- header -----------------------------------------------------------------
REF="${ISSUE_NUM:+#$ISSUE_NUM}"
{
  echo "## Draft — board-pilot run for ${REF:-${ITEM_ID}}. Not merge-ready. You decide."
  echo
  echo "**Asked:** ${ITEM_TITLE}"
  if [ -n "$ISSUE_NUM" ]; then
    echo
    # A closing keyword makes a CONNECTED link (board "Linked pull requests" + the
    # issue's Development panel). A bare URL mention does not.
    echo "Closes #${ISSUE_NUM}"
  fi
} > "$BODY_FILE"

# ---- what changed: the cheapest true statement there is ----------------------
NUMSTAT="$(git diff --numstat "origin/${BASE}...HEAD" || true)"
FILES_N="$(printf '%s' "$NUMSTAT" | grep -c . || true)"
ADDED="$(printf '%s\n' "$NUMSTAT" | awk '{s+=$1} END {print s+0}')"
DELETED="$(printf '%s\n' "$NUMSTAT" | awk '{s+=$2} END {print s+0}')"
{
  echo
  echo "### What changed  [machine — git + GitHub PR-API, independent]"
  echo "\`+${ADDED} −${DELETED}\` across **${FILES_N} file(s)**, \`origin/${BASE}...${BRANCH}\`."
  echo "Cross-checkable independently: \`gh pr view <n> --json additions,deletions,changedFiles\`"
  echo
  printf '%s\n' "$NUMSTAT" | sed 's/^/    /'
} >> "$BODY_FILE"

# ---- what was verified: engine-teed, and labelled for what it is worth -------
V_OUT="$EVIDENCE_DIR/verify/stdout"
V_RC="$EVIDENCE_DIR/verify/exit_code"
{
  echo
  echo "### What was verified  [machine-executed, agent-authored]"
  if [ -s "$V_OUT" ] && [ -s "$V_RC" ]; then
    echo "\`verify.sh\` → exit $(tr -d '[:space:]' < "$V_RC"). Read from the pipe by the engine; the stage never authored it:"
    echo
    sed 's/^/    /' < "$V_OUT"
    echo
    # One sentence per line, unwrapped: these are the load-bearing sentences of the
    # whole dossier, and a sentence split across two echo lines is one a reader cannot
    # grep and a test cannot pin. Markdown wraps them for display anyway.
    echo "> pytest ran for real and the exit code belongs to the engine. BUT the assertions were written by the same agent that wrote the code, in the same run."
    echo ">"
    echo "> Green means self-consistent, not correct."
    echo ">"
    echo "> The only independent evidence in this body is the diff numbers above and the CI below."
  else
    # Honest degradation: no sink, no claim. Never a stand-in sentence that reads green.
    echo "No engine-captured verify output exists for this run, so this body claims NO verification."
  fi
  echo
  echo "**TDD order: NOT verified.** The suite is green now; nothing here shows a test was red"
  echo "first. That is not reconstructable after the fact."
  echo
  echo "**Cost: unmeasured.** The engine hardcodes tokens=0, so no number is printed rather"
  echo "than an invented one. Model not reported: none is pinned, so the engine observes none."
} >> "$BODY_FILE"

# ---- what the reviewer said: a claim, labelled ------------------------------
R_VERDICT="$EVIDENCE_DIR/review/verdict.json"
{
  echo
  echo "### What the reviewer said  [agent — an OPINION, not a verification]"
  if [ -s "$R_VERDICT" ]; then
    python3 - "$R_VERDICT" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except (ValueError, OSError):
    d = {}
print("`review.sh` → verdict **%s**" % (d.get("verdict") or "unknown"))
print()
for line in (d.get("annotation") or "(no annotation)").splitlines() or ["(no annotation)"]:
    print("> " + line)
PY
    echo
    echo "This is a model's self-report on work by the same model family. It is evidence that a"
    echo "review ran and what it concluded — NOT evidence that the code is correct. That is what"
    echo "your review is for."
  else
    echo "No reviewer verdict was captured for this run, so this body reports none."
  fi
} >> "$BODY_FILE"

# ---- rework: the durable counter --------------------------------------------
{
  echo
  echo "### Rework: ${BOUNCES:-0} round(s)  [machine — board Number field \`Bounces\`]"
  echo "Per-round diffs are not recorded; the round-scoped notes on the issue are the full history."
} >> "$BODY_FILE"

# ---- checks: third party, linked not quoted ---------------------------------
{
  echo
  echo "### Checks"
  echo "CI runs **after** this PR opens — this body cannot cite its own CI."
  echo "→ **The Checks tab is authoritative.** Branch protection is enforced by GitHub"
  echo "independently of board-pilot; this gate is not ours to weaken."
} >> "$BODY_FILE"

# ---- where to look first: mechanical, not agent-framed -----------------------
# Ranked OUTSIDE the block, in a command substitution: `sort | head -3` sends SIGPIPE
# to sort as soon as head has its three lines, and under `set -o pipefail` that 141
# fails the whole pipeline. It only bites once a diff has more than three files —
# i.e. never in a smoke test, always in production.
TOP_FILES="$(printf '%s\n' "$NUMSTAT" | sort -rn | head -3 || true)"
{
  echo
  echo "### Where to look first  [computed from the diff, not framed by the agent]"
  while read -r a d f; do
    # An empty NUMSTAT still yields one empty line; `[ -n ]` as the loop's last
    # command would return 1 and take the whole `set -e` script down with it.
    [ -n "${f:-}" ] || continue
    echo "- \`${f}\` — +${a} −${d} (largest change)"
  done <<< "$TOP_FILES"
  # A mechanical signal that points at what the reviewer may have MISSED, rather than
  # at what it liked. An agent-written "start here" would be the model grading its own
  # reviewer, in the one section without a label.
  TESTS_TOUCHED="$(printf '%s\n' "$NUMSTAT" | awk '{print $3}' | grep -cE '(^|/)tests?/|test_|_test\.' || true)"
  if [ "${TESTS_TOUCHED:-0}" -eq 0 ]; then
    echo "- **No test file changed in this diff.** Whatever this change does is untested by it."
  fi
} >> "$BODY_FILE"

# ---- the gate: fail-closed scan BEFORE anything is transported ---------------
# Fail-closed, not redact-and-post: the record sink may redact, but the dossier is the
# artefact a human merges on, and a suspect body is not emitted even redacted.
if ! bp_scan_file "$BODY_FILE"; then
  bp_die "potential secret in the PR body — refusing to open a PR (fail-closed). Nothing was posted."
fi

# ---- transport --------------------------------------------------------------
# Idempotent: this stage is re-dispatched on any tick that still finds the item at
# pr-ready, and a second PR for one branch is not a recoverable state.
PR_URL="$("$GH" pr list --head "$BRANCH" --state open --json url --jq '.[0].url // empty' 2>/dev/null || true)"
if [ -n "$PR_URL" ]; then
  echo "[pr] reusing open PR: $PR_URL" >&2
else
  # --draft: board-pilot never opens a ready PR. --body-file - : the body rides stdin,
  # never argv.
  PR_URL="$("$GH" pr create --draft --head "$BRANCH" --base "$BASE" \
    --title "$ITEM_TITLE" --body-file - < "$BODY_FILE")"
  echo "[pr] created draft PR: $PR_URL" >&2
fi

echo "$PR_URL"
