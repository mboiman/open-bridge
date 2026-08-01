"""The launchd shim must fail CLOSED when the App-token mint fails.

A fail-open shim (mint fails -> unset GH_TOKEN -> ambient gh auth) does not merely
lose a rate-limit bucket: it silently swaps the engine's IDENTITY. The tick then runs
as the human, and the reject-note read-back — which authenticates a note by its author
— starts trusting that human's entire comment history on a public issue. Any drive-by
comment carrying the marker would steer the autonomous code writer. A missed tick is
free (the board is durable state; the next tick re-reads it); running as the wrong
identity is not. So: mint fails -> exit 1, and the tick never starts.

These drive the REAL script (never a copy of its logic) with a stubbed mint helper.
The shim resolves the helper as "$HOME/bin/gh-app-token.sh" — an absolute path — and
overrides PATH itself, so a PATH-shadowed stub would NOT intercept it; HOME is the only
honest seam. The real ~/bin/gh-app-token.sh and the real keychain are never touched.
"""
import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO / "infra" / "remotes" / "homeserver" / "scripts"
SHIM = _SCRIPTS / "board-pilot-run.sh"

# The shim is USER-tier (infra/remotes/<box>/) while this suite is a scope:core skill
# that ships to the OSS upstream, where that box's scripts do not exist. Skip only when
# the whole per-box script dir is absent (= not this instance). If the dir IS here but
# the shim is not, FAIL — that means the guarded file was renamed or deleted out from
# under this test, and a silent skip is exactly how a guard evaporates unnoticed.
pytestmark = pytest.mark.skipif(
    not _SCRIPTS.is_dir(), reason="homeserver script dir absent (USER-tier; not this instance)"
)


def _drive(tmp_path, mint: str | None):
    """Run the real shim with a stubbed mint helper; return (proc, what the exec'd child saw).

    The child is a probe standing in for the plist's python tick. If the shim fails
    closed it is never exec'd, so the ABSENCE of its output file is the proof that no
    token reached the tick — and that no tick ran at all.
    """
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    if mint is not None:
        helper = home / "bin" / "gh-app-token.sh"
        helper.write_text(mint)
        helper.chmod(0o755)

    probe_out = tmp_path / "probe-saw.txt"
    probe = tmp_path / "probe.sh"
    probe.write_text(
        '#!/bin/bash\nprintf "token=%s\\nargv=%s\\n" "${GH_TOKEN-<unset>}" "$*" > "$PROBE_OUT"\n'
    )
    probe.chmod(0o755)

    proc = subprocess.run(
        [str(SHIM), str(probe), "-m", "engine.cli", "--once"],
        env={"HOME": str(home), "PROBE_OUT": str(probe_out), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, (probe_out.read_text() if probe_out.exists() else None)


def test_run_shim_exits_when_mint_fails(tmp_path):
    proc, child_saw = _drive(tmp_path, '#!/bin/bash\necho "mint boom" >&2\nexit 1\n')
    assert proc.returncode != 0, "mint failed but the shim exited 0 — launchd records success"
    assert child_saw is None, f"the tick RAN despite a failed mint; it saw: {child_saw!r}"


def test_run_shim_exits_when_mint_helper_is_missing(tmp_path):
    """An absent helper (fresh box, botched deploy) is a mint failure, not a green light."""
    proc, child_saw = _drive(tmp_path, None)
    assert proc.returncode != 0
    assert child_saw is None


def test_run_shim_exits_when_mint_returns_empty_token(tmp_path):
    """Exit 0 with an empty token is the nastiest case: it LOOKS like a successful mint,
    and the old `[ -n "$TOK" ] &&` guard degraded it into a silent identity swap."""
    proc, child_saw = _drive(tmp_path, "#!/bin/bash\nprintf ''\nexit 0\n")
    assert proc.returncode != 0
    assert child_saw is None


def test_run_shim_execs_tick_with_minted_token_on_success(tmp_path):
    """The control: proves the failure tests above are not vacuously green, and pins the
    argv contract the plist depends on (it passes the python invocation THROUGH the shim)."""
    proc, child_saw = _drive(tmp_path, "#!/bin/bash\nprintf 'ghs_faketoken123'\n")
    assert proc.returncode == 0, f"clean mint must still tick; stderr={proc.stderr!r}"
    assert child_saw is not None, "the shim did not exec the tick after a successful mint"
    assert "token=ghs_faketoken123" in child_saw
    assert "argv=-m engine.cli --once" in child_saw
