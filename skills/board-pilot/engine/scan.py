"""Redaction gate — the one place agent-authored text is scanned before it is posted.

Nothing scanned posted text before this module. open-bridge-analyze.sh scans
`git diff --cached`, which cannot be pointed at a comment body, and the pr stage had
no scan at all. Everything this pipeline posts — reject notes carrying raw reviewer
output, the run record, the PR dossier — lands on a world-readable MIT repo forever.

Two consumers, two postures:
  - the record sink calls scrub() and posts the redacted text; it never blocks a latch
  - the pr stage pipes its body through the CLI, which exits 1 on any hit (fail-closed)

Redaction is per-SPAN, never per-body. A scanner that stubs the whole body on one
false positive blanks the note while Bounces keeps climbing: the producer reworks
against nothing, and the ledger reports a healthy loop the whole time.

HONEST LIMIT — this is a regex denylist, the same class as the bash it lifts from,
not gitleaks (Backlog #2). It catches token SHAPES it already knows. It does not
catch a secret with no distinctive shape (a password, a customer name, an internal
hostname), and a denylist can never enumerate what it has not seen. It is better
than the nothing that scans prose today; it is not a solved problem.
"""
from __future__ import annotations

import re
import sys
from typing import NamedTuple

# Lifted from open-bridge-analyze.sh:57 so there is ONE definition instead of a copy
# per script — a second copy drifts silently, and the drifted half is the one that
# posts. Translated only where POSIX and Python disagree: `[:space:]` → `\s`. The
# bash greps with -i, so IGNORECASE below preserves its semantics rather than
# quietly narrowing them.
_SECRET_RE = re.compile(
    "|".join(
        (
            r"gh[opsur]_[A-Za-z0-9]{20,}",
            r"github_pat_[A-Za-z0-9_]{20,}",
            r"AKIA[0-9A-Z]{16}",
            r"AIza[0-9A-Za-z_-]{35}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"xox[baprs]-[A-Za-z0-9-]+",
            r"sk-[A-Za-z0-9]{20,}",
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
            r"AccountKey=[A-Za-z0-9+/=]{20,}",
            r"SharedAccessSignature=",
            r"://[^/@\s]+:[^/@\s]+@",
        )
    ),
    re.IGNORECASE,
)

# An absolute local path names the human whose box ran the tick and maps his disk.
# The `handler` column was cut from the record for exactly this reason; this rule is
# the backstop for every other field, above all engine-teed stderr, where a traceback
# prints the worker's real paths.
_REPO_ROOT_RE = re.compile(r"/Users/[A-Za-z0-9._-]+(?:/[^\s\"'`)\]]*)*")

_RULES = (("secret", _SECRET_RE), ("repo-root", _REPO_ROOT_RE))


class Hit(NamedTuple):
    """A citation, not a copy: rule + span, never the matched bytes. A hit that
    carried the secret would re-leak it into every log and record that renders it."""

    rule: str
    start: int
    end: int


def scrub(text: str) -> tuple[str, list[Hit]]:
    """Redact every matched span, keep everything between them.

    A body with no hits is returned byte-identically — the gate is invisible until
    it fires. Code fences get NO exemption: the fence is where engine-teed test
    output lives, and a fence-aware scanner would be steerable by the very text it
    scans (open a fence, and the rest goes unread).
    """
    spans = [
        Hit(rule, m.start(), m.end())
        for rule, pattern in _RULES
        for m in pattern.finditer(text)
    ]
    # Earliest start wins, longest span breaks the tie, overlaps are dropped: two
    # rules matching the same bytes must redact once, deterministically, and never
    # interleave two placeholders into one span.
    spans.sort(key=lambda h: (h.start, -(h.end - h.start)))

    out: list[str] = []
    hits: list[Hit] = []
    cursor = 0
    for hit in spans:
        if hit.start < cursor:
            continue
        out.append(text[cursor:hit.start])
        out.append(f"[redacted:{hit.rule}]")
        hits.append(hit)
        cursor = hit.end
    out.append(text[cursor:])
    return "".join(out), hits


def main(argv=None) -> int:
    """Fail-closed filter for the pr stage: `... | python3 -m engine.scan`.

    Exit 0 = clean, body on stdout. Exit 1 = hits, NOTHING on stdout. Any nonzero
    exit means: do not post. A suspect body is not emitted even redacted — the record
    sink may redact and post, but the dossier is the artefact a human merges on.
    """
    import argparse

    argparse.ArgumentParser(
        prog="engine.scan",
        description="Scan a body on stdin. Exit 1 on any hit; the caller must not post.",
    ).parse_args(argv)

    text = sys.stdin.read()
    clean, hits = scrub(text)
    if hits:
        for hit in hits:
            # rule + line, never the matched bytes: a scanner that echoes what it
            # caught re-leaks the secret into every log that reads its stderr.
            print(
                f"board-pilot: {hit.rule} on line {text.count(chr(10), 0, hit.start) + 1}",
                file=sys.stderr,
            )
        print(f"board-pilot: {len(hits)} hit(s) — refusing to emit", file=sys.stderr)
        return 1
    sys.stdout.write(clean)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
