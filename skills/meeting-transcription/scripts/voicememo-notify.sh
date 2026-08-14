#!/usr/bin/env bash
# voicememo-notify.sh: runs on the transcription worker host (worker +
# capture are the same machine on this instance, see
# work/streams/voice-memo-debrief-automation/). Watches
# ~/Transcripts/voicememo/ for finished transcripts and emails each one to
# Michael, unattended — no interactive session involved, matching the other
# mail automations on this box (project-status-notifier's wrapper scripts).
#
# Deliberately the RAW transcript only, no analysis/summarization: that is
# the still-open, still-unconfirmed "Analyse-Schritt" from STATUS.md Next
# Steps. This script only closes the narrower, already-confirmed loop —
# "send me an email once a recording has been transcribed".
#
# Idempotent via a marker file per transcript (same pattern as
# voicememo-bundler.sh's pushed/<basename>.done), independent of
# debrief_sync.sh pull's own _debriefed/ move — this script only READS
# ~/Transcripts/voicememo/, never moves/deletes anything there, so pull
# still works unmodified.
#
# Drop-in: ~/bin/voicememo-notify.sh   Logs: ~/Library/Logs/voicememo-notify.log
# Triggered by launchd WatchPath (com.bks.voicememo-notify.plist).
#
# NOTE: EMAIL_OPS below intentionally stays a $HOME/.claude path (this script's
# own long-standing convention, independent of any particular Bridge checkout
# location). RECIPIENT is different: unlike EMAIL_OPS it names one specific
# person, so it is read from bridge-config.yaml instead: this script now
# assumes it runs from its own position inside a Bridge checkout for that
# one lookup (same assumption voicememo-bundler.sh, debrief_sync.sh and
# add_context.sh already make about their own location), resolved from
# ${BASH_SOURCE[0]} at runtime, never a hardcoded address.

set -euo pipefail

TRANSCRIPTS_DIR="$HOME/Transcripts/voicememo"
NOTIFIED_DIR="$HOME/Library/Application Support/bridge-voicememo/notified"
EMAIL_OPS="$HOME/.claude/skills/email-manager/scripts/email_ops.py"

# Repo root resolved from this script's own location (mirrors debrief_sync.sh,
# add_context.sh, voicememo-bundler.sh), never a hardcoded path or username.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CONFIG="$REPO_ROOT/bridge-config.yaml"

# Read a dotted key from a YAML file (mirrors debrief_sync.sh / add_context.sh).
yaml_get() {
  [[ -f "$1" ]] || return 0
  python3 -c '
import sys
try:
    import yaml
    data = yaml.safe_load(open(sys.argv[1])) or {}
except Exception:
    sys.exit(0)
node = data
for part in sys.argv[2].split("."):
    if not isinstance(node, dict) or part not in node:
        sys.exit(0)
    node = node[part]
if node is not None and not isinstance(node, (dict, list)):
    print(node)
' "$1" "$2" 2>/dev/null || true
}

# No hardcoded personal address: see integrations.transcription.notify_email
# in bridge-config.yaml (docs/transcription-worker.md documents the field).
RECIPIENT="$(yaml_get "$CONFIG" integrations.transcription.notify_email)"

LOG="$HOME/Library/Logs/voicememo-notify.log"

mkdir -p "$NOTIFIED_DIR"
mkdir -p "$(dirname "$LOG")"

log() { printf '%s [voicememo-notify] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG"; }

already_notified() { [[ -f "$NOTIFIED_DIR/$(basename "$1").done" ]]; }
mark_notified() { touch "$NOTIFIED_DIR/$(basename "$1").done"; }

if [[ -z "$RECIPIENT" ]]; then
  log "ERROR no notification recipient configured: set integrations.transcription.notify_email in bridge-config.yaml"
  exit 0
fi

[[ -d "$TRANSCRIPTS_DIR" ]] || { log "ERROR transcripts dir missing: $TRANSCRIPTS_DIR"; exit 0; }

shopt -s nullglob
sent=0
for f in "$TRANSCRIPTS_DIR"/*.md; do
  [[ -f "$f" ]] || continue
  base="$(basename "$f")"
  already_notified "$f" && continue

  body_file="$(mktemp)"
  {
    printf 'Neue Sprachaufnahme transkribiert: %s\n\n' "$base"
    cat "$f"
  } > "$body_file"

  if python3 "$EMAIL_OPS" send \
      --to "$RECIPIENT" \
      --subject "Sprachmemo transkribiert: $base" \
      --body "@$body_file" >> "$LOG" 2>&1; then
    mark_notified "$f"
    sent=$((sent + 1))
    log "sent $base"
  else
    log "ERROR sending $base"
  fi
  rm -f "$body_file"
done

# if/fi, not a bare `(( sent > 0 )) && log ...`: under set -e, that bare form
# makes the script's own exit code track whether anything was sent — a normal
# "nothing new" run (sent=0) would then exit 1, misreported by launchd as a
# job failure even though nothing went wrong.
if (( sent > 0 )); then
  log "done — $sent notification(s) sent"
fi
