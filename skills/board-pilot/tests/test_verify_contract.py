"""verify.sh's exit-code contract, pinned edge by edge — the infra/rework line.

Why this file exists: a live run parked an item after this stage's fallback
guessed `python3 -m pytest -q` on a box whose python3 has no pytest. The guess
exited 1, rc=1 was classified as a red suite, and the reject note "No module
named pytest" was handed to the implement agent — which correctly changed
nothing, because no repo change can install a module on the runner box. An
INFRA failure rode the reject edge and burned a paid LLM round.

Three contract holes, each pinned here:

  1. The fallback heuristic. A guessed suite ties the verdict to the runner
     box's CWD and environment — the guess itself can be the red. Unset or
     empty BP_VERIFY_CMD must die naming the knob, never guess.
  2. The vacuous command line. A whitespace-only or comment-only BP_VERIFY_CMD
     satisfies a bare non-empty check, and `bash -c " "` exits 0 — a permanent
     green that no suite ever produced, which even the deploy-time baseline
     gate would confirm. Stripped-empty or leading-# must die in the same
     message class as unset.
  3. rc=126/127 from `bash -c "$VERIFY_CMD"` mean the command COULD NOT RUN
     (not executable / not found), and rc >= 128 means the suite was KILLED by
     signal rc-128 (OOM 137, SIGTERM 143) — in both, the suite never judged
     the diff. That is an infra crash: exit non-zero so the engine takes
     on_fail (park), never the reject edge. rc=1..125 stays a review verdict —
     the suite ran and has a finding.

Driven through the REAL script, same posture as tests/test_stages.py: a
throwaway git repo with a local bare origin, `bash <the real file>`, no network.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
STAGES = SKILL_ROOT / "stages"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        # Never read the developer's ~/.gitconfig: identity and hooks must not leak
        # into a test repo.
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo with a local bare origin, shaped like a pytest project.

    `tests/` AND `pyproject.toml` are both present ON PURPOSE: this is the exact
    shape the removed fallback heuristic keyed on. The no-knob test below only
    proves the heuristic is GONE if the repo would have matched it.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "verify-contract@example.invalid")
    _git(work, "config", "user.name", "Verify Contract Test")
    (work / "tests").mkdir()
    (work / "pyproject.toml").write_text("[project]\nname = \"fixture\"\nversion = \"0\"\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    return work


@pytest.fixture
def env(tmp_path):
    """The env the engine's runner really exports to the verify stage.

    BP_VERIFY_CMD is stripped from the inherited environment: every test states
    its own, and the no-knob tests must not pass or fail on the developer's shell.
    """
    e = {
        **os.environ,
        "ITEM_ID": "ITEM1",
        "BRANCH": "bridge/demo/ITEM1",
        "VERDICT_FILE": str(tmp_path / "verdict.json"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    e.pop("BP_VERIFY_CMD", None)
    return e


def _run_verify(env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(STAGES / "verify.sh")],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------- no knob, no guess
@pytest.mark.parametrize("shape", ["unset", "empty", "whitespace", "comment", "indented-comment"])
def test_missing_verify_cmd_dies_naming_the_knob_never_guesses(shape, env, repo):
    """The repo matches everything the old fallback keyed on (tests/, pyproject.toml),
    so a surviving heuristic would run a guessed suite and exit 0 here. The contract
    is: no BP_VERIFY_CMD means the stage CANNOT verify — refuse, and tell the
    operator exactly which knob to set and where it lives.

    whitespace/comment shapes pin the vacuous-green hole: `bash -c " "` and
    `bash -c "#anything"` both exit 0 without running any suite, so a bare
    non-empty check turns a config typo into a permanent green verdict."""
    if shape == "empty":
        env["BP_VERIFY_CMD"] = ""
    elif shape == "whitespace":
        env["BP_VERIFY_CMD"] = " \t "
    elif shape == "comment":
        env["BP_VERIFY_CMD"] = "#comment"
    elif shape == "indented-comment":
        env["BP_VERIFY_CMD"] = "  # exit 0"
    r = _run_verify(env, repo)

    assert r.returncode != 0, "a stage with no verify command must refuse, never guess one"
    # Name the knob AND its location: a bare `!= 0` is satisfied by rc=127 from a
    # missing script, and a message without the location leaves the operator hunting.
    assert "BP_VERIFY_CMD" in r.stderr
    assert "unit env" in r.stderr
    assert not Path(env["VERDICT_FILE"]).exists(), "a refusal must not leave a reject note behind"


# ---------------------------------------------------------------- rc=126/127 -> infra
def test_rc_127_command_not_found_is_infra_crash_not_reject(env, repo):
    """rc=127 = the command does not exist on this box. Nothing in the repo can fix
    that, so a reject verdict here buys a paid rework round that must end in an
    empty diff. Non-zero exit -> on_fail -> park is the only honest route."""
    env["BP_VERIFY_CMD"] = "bp-test-no-such-binary-deliberately-missing"
    r = _run_verify(env, repo)

    assert r.returncode != 0, "cannot-execute must take the on_fail edge, not the reject edge"
    assert "rc=127" in r.stderr, "the refusal must name the rc so the operator sees cannot-execute"
    assert not Path(env["VERDICT_FILE"]).exists(), (
        "an infra crash must never become a reject note for the implement agent"
    )
    # The engine tees stdout for evidence — the shell's own diagnosis must reach it.
    # Asserted by the command name, not by "not found": bash localizes that phrase.
    assert "bp-test-no-such-binary-deliberately-missing" in r.stdout


def test_rc_126_not_executable_is_infra_crash_not_reject(env, repo, tmp_path):
    """rc=126 = the command resolved but cannot execute (permission bit, wrong
    interpreter). Same class as 127: the SUITE never ran, so there is no finding
    about the diff to report."""
    suite = tmp_path / "suite.sh"
    suite.write_text("#!/bin/bash\nexit 0\n")  # deliberately no exec bit
    env["BP_VERIFY_CMD"] = str(suite)
    r = _run_verify(env, repo)

    assert r.returncode != 0
    assert "rc=126" in r.stderr
    assert not Path(env["VERDICT_FILE"]).exists()


def test_rc_137_signal_killed_suite_is_infra_crash_not_reject(env, repo):
    """rc=137 = the suite was killed by SIGKILL (128+9: the OOM killer, a timeout
    wrapper). No repo change can prevent a kill, so a reject verdict here buys a
    paid rework round that must end in an empty diff — the same class as 126/127.
    The refusal must name both the rc and the decoded signal number."""
    env["BP_VERIFY_CMD"] = "echo 'suite started'; exit 137"
    r = _run_verify(env, repo)

    assert r.returncode != 0, "a signal-killed suite must take the on_fail edge, not the reject edge"
    assert "rc=137" in r.stderr, "the refusal must name the rc"
    assert "signal 9" in r.stderr, "the refusal must decode rc-128 so the operator sees the kill"
    assert not Path(env["VERDICT_FILE"]).exists(), (
        "a kill must never become a reject note for the implement agent"
    )


# ---------------------------------------------------------------- rc=1..125 -> reject
def test_rc_1_red_suite_still_writes_reject_verdict_and_exits_zero(env, repo):
    """The boundary must not widen: rc=1 is a suite that RAN and found something.
    That is a review verdict — reject sidecar written, exit 0 — because only
    ok=True reaches the capped reject edge (claude_runner consults the sidecar
    `if stage.reject_to and result.ok`)."""
    env["BP_VERIFY_CMD"] = (
        f"{sys.executable} -c \"import sys; sys.stdout.write('1 failed, 4 passed'); sys.exit(1)\""
    )
    r = _run_verify(env, repo)

    assert r.returncode == 0, f"a red suite must still be a CLEAN RUN: {r.stderr}"
    verdict = json.loads(Path(env["VERDICT_FILE"]).read_text())
    assert verdict["verdict"] == "reject"
    assert "1 failed" in verdict["annotation"], "the annotation must carry the real output"


def test_rc_125_stays_on_the_reject_edge(env, repo):
    """The upper edge of the reject band, pinned so the infra classification can
    never creep down into codes a suite can legitimately return."""
    env["BP_VERIFY_CMD"] = "echo 'suite ran'; exit 125"
    r = _run_verify(env, repo)

    assert r.returncode == 0
    assert json.loads(Path(env["VERDICT_FILE"]).read_text())["verdict"] == "reject"


# ---------------------------------------------------------------- green
def test_green_suite_exits_zero_with_no_verdict(env, repo):
    """The negative control: a green run writes nothing and exits 0, and its
    output still reaches stdout for the engine's evidence tee."""
    env["BP_VERIFY_CMD"] = f"{sys.executable} -c \"print('5 passed in 0.01s')\""
    r = _run_verify(env, repo)

    assert r.returncode == 0, r.stderr
    assert not Path(env["VERDICT_FILE"]).exists(), "a green run must not write a verdict"
    assert "5 passed" in r.stdout
