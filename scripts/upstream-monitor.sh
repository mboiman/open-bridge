#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# upstream-monitor.sh — daily upstream drift + CORE-conflict monitor.
#
# Fetches the upstream remote, runs bridge-divergence-check.py, writes
# work/upstream-status.md (picked up by /briefing), and — if SIGNAL_ACCOUNT +
# SIGNAL_RECIPIENT are set (see the launchd plist) — sends a one-line Signal
# summary so you can flag the lead dev early.
#
# CORE-generic: no instance values baked in. Notification creds come from env,
# set by the per-instance launchd job (infra/, USER tier).
#
# Env knobs (all optional):
#   UPSTREAM_REMOTE (default: upstream)   UPSTREAM_REF (default: upstream/main)
#   SIGNAL_ACCOUNT   SIGNAL_RECIPIENT     (both required to enable Signal push)
set -uo pipefail
cd "$(dirname "$0")/.."

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_REF="${UPSTREAM_REF:-upstream/main}"
REPORT="work/upstream-status.md"

git fetch "$UPSTREAM_REMOTE" -q 2>/dev/null || true

JSON=$(python3 scripts/bridge-divergence-check.py --upstream "$UPSTREAM_REF" --json 2>/dev/null)
if [ -z "$JSON" ]; then echo "monitor: divergence-check produced no output" >&2; exit 1; fi
read -r behind diverged risk <<<"$(printf '%s' "$JSON" | python3 -c \
  'import sys,json;d=json.load(sys.stdin);print(d["behind"],len(d["local_core_edits"]),len(d["conflict_risk"]))')"

# themed summary of the incoming commits (conventional-commit type prefixes)
summary=$(git log --oneline --no-decorate "HEAD..$UPSTREAM_REF" 2>/dev/null \
  | sed -E 's/^[0-9a-f]+ //' | grep -oiE '^[a-z]+(\(|:)' | sed -E 's/[(:]//' \
  | sort | uniq -c | sort -rn | head -6 | awk '{printf "%s×%s ",$1,$2}')

mkdir -p work
{
  echo "# Upstream Status — $(date '+%Y-%m-%d %H:%M')"
  echo
  echo "- **Behind \`$UPSTREAM_REF\`:** $behind commits"
  echo "- **CORE divergence:** $diverged files · **conflict-risk on merge:** $risk"
  echo "- **Incoming themes:** ${summary:-–}"
  echo
  if [ "${risk:-0}" -gt 0 ]; then
    echo "## ⚠ Conflict-risk files — resolve before \`git merge $UPSTREAM_REF\`"
    printf '%s' "$JSON" | python3 -c 'import sys,json;[print("- `"+f+"`") for f in json.load(sys.stdin)["conflict_risk"]]'
    echo
  fi
  echo "## Recent incoming commits (newest 15)"
  git log --oneline --no-decorate "HEAD..$UPSTREAM_REF" 2>/dev/null | head -15 | sed 's/^/- /'
} > "$REPORT"

if [ -n "${SIGNAL_ACCOUNT:-}" ] && [ -n "${SIGNAL_RECIPIENT:-}" ] && command -v signal-cli >/dev/null 2>&1; then
  emoji="⬆"; [ "${risk:-0}" -gt 0 ] && emoji="⚠"
  msg="$emoji open-bridge upstream: ${behind} behind · ${risk} conflict-risk · ${summary:-–}"
  if signal-cli -a "$SIGNAL_ACCOUNT" send -m "$msg" "$SIGNAL_RECIPIENT" >/dev/null 2>&1; then
    echo "signal: sent"
  else
    echo "signal: send FAILED" >&2
  fi
fi
echo "monitor: wrote $REPORT (behind=$behind risk=$risk)"
