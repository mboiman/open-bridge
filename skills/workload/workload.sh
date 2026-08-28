#!/bin/sh
# Entry point of the workload skill.
#
# The skill is reached through the discovery symlink, so $0 is usually a link.
# Resolve it step by step: readlink -f is not portable, and this loop is.
set -eu

target="$0"
while [ -L "$target" ]; do
    link="$(readlink "$target")"
    case "$link" in
        /*) target="$link" ;;
        *)  target="$(dirname "$target")/$link" ;;
    esac
done
SKILL_DIR="$(cd "$(dirname "$target")" && pwd -P)"

# WHICH python3 runs this, and why it is not simply the first one on PATH.
#
# Measured on 2026-08-25: on the machine this skill exists to watch, a non
# interactive PATH resolves `python3` to the system one, which has no PyYAML,
# while a usable interpreter sits right beside it. The skill did not start at
# all there, and the failure was a bare ModuleNotFoundError from the middle of
# an import chain, which says nothing about the actual problem and sends the
# reader into the wrong file.
#
# Neither may be hardcoded, and the repository already carries both scars:
# `/usr/bin/python3` is a forwarder that resolves differently per machine, and
# a versioned path is deleted by the very upgrade that creates its successor.
#
# So the candidates are PROBED, and the probe measures the requirement itself
# rather than a stand-in for it: it imports the skill. A version number would
# be a second derivation of "can this run" and drifts the day a dependency
# changes.
can_run() {
    PYTHONPATH="$SKILL_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$1" -c 'import engine.cli' >/dev/null 2>&1
}

CANDIDATES="python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3"

refuse() {
    echo "workload: no python3 here can run this skill." >&2
    echo >&2
    if [ -n "${BRIDGE_PYTHON:-}" ]; then
        echo "  BRIDGE_PYTHON names $BRIDGE_PYTHON, and it cannot." >&2
    fi
    echo "  Each candidate has to import the skill, which needs PyYAML (import yaml)." >&2
    echo "  What was found:" >&2
    for cand in $CANDIDATES; do
        full="$(command -v "$cand" 2>/dev/null || true)"
        if [ -z "$full" ]; then
            echo "    $cand: not there" >&2
            continue
        fi
        version="$("$full" -V 2>&1 || echo '?')"
        if "$full" -c 'import yaml' >/dev/null 2>&1; then
            echo "    $full: $version, yaml ok, but cannot import the skill" >&2
        else
            echo "    $full: $version, no yaml" >&2
        fi
    done
    echo >&2
    echo "  Install PyYAML for one of them, or set BRIDGE_PYTHON to name one." >&2
    exit 78
}

if [ -n "${BRIDGE_PYTHON:-}" ]; then
    # Named outright wins, because a machine with an unusual layout needs a way
    # to say what to run. It is still probed, so a wrong one is answered with
    # the report above instead of an import error out of nowhere.
    can_run "$BRIDGE_PYTHON" || refuse
    PYTHON="$BRIDGE_PYTHON"
else
    PYTHON=""
    for cand in $CANDIDATES; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if can_run "$cand"; then PYTHON="$cand"; break; fi
    done
    [ -n "$PYTHON" ] || refuse
fi

PYTHONPATH="$SKILL_DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$PYTHON" -m engine.cli "$@"
