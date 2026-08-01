"""V1 — the redaction gate: nothing agent-authored reaches a public comment unscanned.

Everything this pipeline posts lands on a world-readable MIT repo and stays there.
The NEGATIVE path is the load-bearing one: a rule broad enough to redact everything
passes every positive test in this file and fails only
test_benign_body_passes_through_unchanged. That test is why this file exists.

Token-shaped samples are assembled at runtime, never written as literals: a literal
here would be caught by GitHub push protection — and by this very scanner, run over
its own repo — in the one file whose job is to carry them.
"""
import subprocess
import sys
from pathlib import Path

from engine.scan import Hit, scrub

_SKILL_ROOT = Path(__file__).resolve().parents[1]

# assembled at runtime — see the module docstring
_FAKE_GH_TOKEN = "gh" + "p_" + "0123456789abcdefghij0123"
_FAKE_AWS_KEY = "AKIA" + "0123456789ABCDEF"

# A realistic record entry: markdown table, code fence, URL, issue ref. This is the
# shape the record sink actually posts, so it is the shape the gate must not touch.
_BENIGN_RECORD = """\
<!-- board-pilot:run item=PVTI_lADOB6Rsr84BbwLTzgw9yEQ -->
## board-pilot run record — #114

| when (Europe/Berlin) | stage | kind | criteria | took | outcome |
|---|---|---|---|---:|---|
| 2026-07-15T14:19:19+02:00 | verify | machine | — | 2m08s | **reject** → implementing · round 1/3 |
| 2026-07-15T14:30:33+02:00 | verify | machine | — | 2m11s | pass → reviewing |

Closes #114 · Checks: https://github.com/bks-lab/open-bridge/pull/118/checks

```console
$ python3 -m pytest tests/test_scan.py -q
.....                                                              [100%]
5 passed in 0.15s
```

See `engine/scan.py:41` for the changed public signature.
"""


def _run_cli(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "engine.scan"],
        input=body,
        capture_output=True,
        text=True,
        cwd=str(_SKILL_ROOT),
    )


# 1) the negative path — the reason this gate is a span redactor ---------------
def test_benign_body_passes_through_unchanged():
    """THE test. A rule that redacts everything passes every positive test in this
    file and fails only here. A realistic record entry must come back byte-identical
    with zero hits, or the scanner destroys the record this layer exists to create."""
    clean, hits = scrub(_BENIGN_RECORD)
    assert hits == []
    assert clean == _BENIGN_RECORD


# 2) span redaction — the match dies, the surrounding record lives -------------
def test_repo_root_path_is_redacted_but_context_survives():
    """Stubbing the whole body on one hit blanks the note while Bounces still climbs:
    the producer reworks against nothing and the ledger reports a healthy loop."""
    text = "See /Users/dev/work/repo/engine/scan.py:41 for the signature."
    clean, hits = scrub(text)
    assert [h.rule for h in hits] == ["repo-root"]
    assert "alice" not in clean
    assert clean == "See [redacted:repo-root] for the signature."


def test_secret_span_is_redacted_and_surrounding_prose_survives():
    text = f"The runner exported {_FAKE_GH_TOKEN} into the env, which is the bug."
    clean, hits = scrub(text)
    assert [h.rule for h in hits] == ["secret"]
    assert _FAKE_GH_TOKEN not in clean
    assert clean == "The runner exported [redacted:secret] into the env, which is the bug."


# 3) the fence edge — a decision, not an oversight -----------------------------
def test_repo_root_inside_code_fence_is_still_redacted():
    """A code fence earns NO exemption. The evidence sink is engine-teed stdout of a
    cmd: stage and the dossier prints it INSIDE a fence — a pytest traceback from the
    worker carries absolute paths, so the fence is exactly where this leak lives, not
    the exception to it. A fence-aware scanner would also be steerable by the text it
    scans: open a fence and everything after it goes unscanned — the same position-blind
    hole as a marker parser that does not anchor at byte 0."""
    text = (
        "```console\n"
        "$ python3 -m pytest -q\n"
        "E   FileNotFoundError: /Users/dev/work/repo/x.py\n"
        "```\n"
    )
    clean, hits = scrub(text)
    assert [h.rule for h in hits] == ["repo-root"]
    assert "/Users/" not in clean
    assert "E   FileNotFoundError: [redacted:repo-root]" in clean
    assert clean.startswith("```console\n$ python3 -m pytest -q\n")


# 4) the hit is a citation, never a copy of the secret -------------------------
def test_hit_carries_rule_and_span_but_never_the_matched_bytes():
    """The record cites `2 redactions (repo-root)` and the CLI logs a rule + a line.
    Neither may carry the matched bytes: a hit that stores the secret re-leaks it
    into every log and every record that renders it. The equality below pins that
    shape structurally — a `text` field cannot be added without breaking it."""
    text = f"prefix {_FAKE_GH_TOKEN} suffix"
    _, hits = scrub(text)
    assert hits == [Hit("secret", 7, 7 + len(_FAKE_GH_TOKEN))]


# 5) CLI mode — the pr stage's fail-closed pipe --------------------------------
def test_cli_mode_exits_nonzero_on_hit():
    """The pr stage pipes its dossier through this: a hit means no PR and a park,
    so the exit code is the entire contract."""
    proc = _run_cli(f"## Draft\n\nThe key is {_FAKE_AWS_KEY}\n")
    assert proc.returncode == 1
    assert proc.stdout == ""  # a suspect body is never emitted — not even redacted
    assert "secret" in proc.stderr
    assert _FAKE_AWS_KEY not in proc.stderr  # the scanner never echoes what it caught


def test_cli_mode_passes_clean_body_through_byte_identically():
    proc = _run_cli(_BENIGN_RECORD)
    assert proc.returncode == 0
    assert proc.stdout == _BENIGN_RECORD
