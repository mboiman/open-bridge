#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression tests for the overlay leak scanner's opaque-credential detection.

Origin: on 2026-08-01 a live Elastic Cloud API key sat in a tracked .py file
(`API_KEY = "<base64>"`) and passed every gate. The scanner runs only its
high-precision FORMAT patterns on code files — the generic key:value heuristic
is deliberately off there to avoid false positives on `api_key = get_key()` —
and no format pattern existed for a credential with no distinctive prefix.

These tests pin both halves of the fix:
  1. `decoded_id_secret()` catches base64 blobs that decode to `<id>:<secret>`.
  2. It stays quiet on ordinary base64 payloads, so it is safe to run on code.
  3. `leak_check()` — the real entry point — flags such a line in a .py file.

Run: python3 -m pytest scripts/tests/test_overlay_secret_scan.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("overlay", REPO / "scripts" / "overlay.py")
assert _spec and _spec.loader, "cannot load scripts/overlay.py"
overlay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(overlay)

# The shape that leaked: base64 of `<20-char id>:<22-char secret>`, with no
# prefix a format regex could anchor on — an Elastic Cloud ApiKey, and the same
# shape elsewhere. The real value is deliberately NOT quoted here, not even to
# illustrate: an adversarial pre-publication review found it on 2026-08-01 in
# exactly this comment, sitting in a file the router had marked publishable to
# the PUBLIC repo. Every gate missed it — the token blocklist has no entry for a
# random string, and decoded_id_secret() below hunts base64 BLOBS, so it is blind
# to its own quarry written out in plaintext. Describe the shape; never quote it.
import base64  # noqa: E402  (after the dynamic import above, by design)

_SAMPLE = base64.b64encode(b"aB3xK9mQ2pLr7TvW1zYc:Ef5HjN8sQ4uX0dR6gT2bV").decode()  # pragma: allowlist secret


# --------------------------------------------------------------------- catches
def test_detects_base64_id_secret_in_code_assignment():
    line = f'API_KEY = "{_SAMPLE}"'
    assert overlay.decoded_id_secret(line) == _SAMPLE


def test_detects_regardless_of_surrounding_syntax():
    for line in (
        f"Authorization: ApiKey {_SAMPLE}",
        f"  api_key: {_SAMPLE}",
        f"# leftover key {_SAMPLE}",
        f"curl -H 'Authorization: ApiKey {_SAMPLE}' https://example.test",
    ):
        assert overlay.decoded_id_secret(line) == _SAMPLE, line


# -------------------------------------------------------------------- is quiet
def test_ignores_ordinary_base64_payloads():
    """Binary, hashes, JSON and single-token blobs must not fire.

    A scanner that cries wolf on every long string gets disabled, so the
    quiet-on-data property is as load-bearing as the catch.
    """
    benign = [
        'png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"',
        'sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"',
        # base64 of plain A-Z a-z: decodes cleanly but has no `:` separator
        'blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJz"',
        # base64 of JSON — decodes to ASCII, but not two opaque tokens
        'coords = "eyJ4Ijo4MTUuMDc4MTI1LCJ5Ijo2MC4wfQ=="',
        "token: ${KIBANA_API_KEY}",
        'api_key = os.environ["KIBANA_API_KEY"]',
        "",
    ]
    for line in benign:
        assert overlay.decoded_id_secret(line) is None, line


def test_clean_across_every_tracked_file():
    """The whole repo must be quiet — otherwise the gate is noise, not a gate."""
    import subprocess

    listing = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    offenders = []
    for rel in listing:
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if rel == "scripts/tests/test_overlay_secret_scan.py":
                continue  # this file constructs a sample on purpose
            if overlay.decoded_id_secret(line):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, f"opaque credentials in tracked files: {offenders}"


# ------------------------------------------------------------ real entry point
def test_leak_check_flags_credential_in_a_code_file():
    """leak_check() is what the overlay path actually calls — pin it, not just the helper."""
    content = f'#!/usr/bin/env python3\nAPI_KEY = "{_SAMPLE}"\n'.encode()
    reasons = overlay.leak_check(content, target_scope="org", dest="scripts/thing.py")
    assert any("raw secret material" in r for r in reasons), reasons


def test_leak_check_stays_quiet_on_an_env_reference():
    content = b'#!/usr/bin/env python3\nAPI_KEY = os.environ["KIBANA_API_KEY"]\n'
    reasons = overlay.leak_check(content, target_scope="org", dest="scripts/thing.py")
    assert not any("raw secret material" in r for r in reasons), reasons


# ---------------------------------------------------------------- plaintext form
# The gap the base64 detector above cannot see. Found 2026-08-01 by an
# adversarial pre-publication review: the live Elastic key sat in PLAINTEXT in a
# comment in THIS file — placed here by the same commit that removed its base64
# form from the script, and marked publishable to the PUBLIC repo. Both the
# token blocklist (no entry matches a random string) and decoded_id_secret
# (hunts base64 blobs) were structurally blind to it.
_PLAIN = "aB3xK9mQ2pLr7TvW1zYc:Ef5HjN8sQ4uX0dR6gT2bV"  # pragma: allowlist secret


@pytest.mark.parametrize("line", [
    "{}",
    "# base64(\"{}\") — the shape that leaked.",
    "API_KEY = \"{}\"",
    "  # leftover: {}",
    "Authorization: ApiKey {}",
])
def test_detects_plaintext_id_secret(line: str):
    assert overlay.plaintext_id_secret(line.format(_PLAIN)) == _PLAIN, (
        "a plaintext id:secret pair went undetected. The base64 detector cannot "
        "see this form, so nothing else does either."
    )


def test_plaintext_detector_runs_inside_comments():
    """The real find was in a `#` comment, which the assignment heuristic strips."""
    assert overlay.plaintext_id_secret(f"    # irgendein Hinweis {_PLAIN}") is not None


@pytest.mark.parametrize("line", [
    "https://user:supersecretpassword@example.test/path",   # inside a URL
    "host: macminim4.local:8080",                            # host:port
    "see docs/architecture:overview for the rationale",      # prose with a colon
    "timestamp: 2026-08-01T09:53:12",                        # timestamp
    "path: /Users/alice/work/repo:/opt/homebrew/bin",    # PATH-like
    "key: allLowercaseNoDigitsHere:alsoAllLowercaseHere",    # no entropy mix
    "ID_ONLY: aB3xK9mQ2pLr7TvW1zYc",                         # single token
])
def test_plaintext_detector_ignores_ordinary_lines(line: str):
    assert overlay.plaintext_id_secret(line) is None, (
        f"false positive on: {line!r}. A scanner that cries wolf gets switched off."
    )


def test_pragma_exempts_a_deliberate_fixture():
    assert overlay.plaintext_id_secret(
        f'SAMPLE = "{_PLAIN}"  # pragma: allowlist secret'
    ) is None


def test_leak_reason_never_echoes_the_whole_pair():
    """A reason string that quotes the secret defeats the guard that produced it."""
    reasons = overlay.leak_check(
        f"# leftover {_PLAIN}\n".encode(), target_scope="org", dest="notes.md"
    )
    assert any("raw secret material" in r for r in reasons), (
        f"the plaintext pair passed leak_check: {reasons}"
    )
    joined = " ".join(reasons)
    assert _PLAIN not in joined, f"leak_check echoed the full credential: {joined}"


def test_this_file_carries_no_live_credential():
    """Self-check — the failure that created this section must not recur here."""
    text = pathlib.Path(__file__).read_text(encoding="utf-8")
    offenders = [
        (n, ln) for n, ln in enumerate(text.splitlines(), 1)
        if overlay.plaintext_id_secret(ln) or overlay.decoded_id_secret(ln)
    ]
    assert not offenders, (
        "this test file carries an unpragma'd credential shape at "
        f"line(s) {[n for n, _ in offenders]}. Describe the shape; never quote it."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
