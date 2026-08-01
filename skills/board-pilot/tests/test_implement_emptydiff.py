"""implement.sh — the empty-staged-diff guard must be two-armed.

The single-armed guard ("empty diff = the stage did not implement") is right on a
FRESH branch and wrong on a REWORK round: bp_ensure_branch reuses the item's branch
by design, so after a prior round the branch already carries the work. A rejection
whose cause lives outside the repo (both live runs: a missing module on the runner
box) then makes EVERY new diff empty — and a guard that dies on that can never
converge, no matter how many times the item is re-armed.

Two more properties of the ahead arm, both pinned here because implement.sh is the
ONLY stage that pushes:

* It must PUSH before exiting 0. Round 1 can commit and then fail the push (set -e
  kills the stage after the commit); the retry round no-ops, the diff is empty, and
  without a push here origin never gets the commit — verify greens the LOCAL tree
  and pr.sh opens a PR on a missing or stale remote branch. A push of already-pushed
  commits is a no-op, so pushing unconditionally is safe and heals the failed push.
* It must ride ONLY pipeline-authored work. Every commit this stage mints carries
  "board-pilot <ITEM_ID>" in its body; any commit ahead of base without that marker
  is foreign (junk pushed by someone with repo access), and the stage must refuse
  rather than carry it to the PR gate on the back of a do-nothing round.

Same discipline as test_stages.py: stubs on absolute BP_* overrides, a throwaway
git repo whose `origin` is a local bare repo, and `bash <the real file>`. The
fixtures are mirrored from test_stages.py rather than imported — each stage-test
file must stay runnable alone, and a cross-file import couples both to pytest's
sys.path insertion order.
"""
import os
import subprocess
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
STAGES = SKILL_ROOT / "stages"
CRITERIA = SKILL_ROOT / "criteria"

BRANCH = "bridge/demo/ITEM1"


# ---------------------------------------------------------------- helpers
def _stub(bin_dir: Path, name: str, body: str) -> Path:
    """A stub binary that records how it was called. Absolute, so a script that
    pins its own PATH still finds it via the BP_* override."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)
    return p


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        # Never read the developer's ~/.gitconfig: identity and hooks must not leak
        # into a test repo, and `git config --global` in an agent has burned us before.
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    return r.stdout.strip()


def _code(name: str) -> str:
    """The script minus its comment lines — rules about what a script DOES must not
    be satisfiable by a comment describing the behaviour it never has."""
    body = (STAGES / name).read_text()
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo with an `origin` that is itself a local bare repo.

    A real remote is what makes `git fetch origin` / `git push` exercisable without
    the network — "did not push" is only meaningful if a push could otherwise land.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "stage-test@example.invalid")
    _git(work, "config", "user.name", "Stage Test")
    (work / "README.md").write_text("# fixture\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    return work


@pytest.fixture
def env(tmp_path, repo):
    """The env the engine's ClaudeStageRunner really exports to a `cmd:` stage."""
    ev = tmp_path / "evidence" / "ITEM1"
    ev.mkdir(parents=True)
    return {
        **os.environ,
        "ITEM_ID": "ITEM1",
        "ITEM_TITLE": "Empty rework diff parks an item whose work already exists",
        "ITEM_URL": "https://github.com/example/repo/issues/120",
        "BRANCH": BRANCH,
        "PROJECT": "demo",
        "BOUNCES": "0",
        "REJECTION_NOTE": "",
        "REJECTION_NOTE_FILE": str(tmp_path / "note.txt"),
        "EVIDENCE_DIR": str(ev),
        "CRITERIA_FILE": str(CRITERIA / "implement.md"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def _run(env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(STAGES / "implement.sh")],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        # A stub that reads stdin must never inherit pytest's — it would block.
        stdin=subprocess.DEVNULL,
    )


def _noop_claude(bin_dir: Path) -> Path:
    """A do-nothing agent: the honest output of a rework round whose rejection
    names a cause no repo change can fix."""
    return _stub(bin_dir, "claude", "printf 'nothing to change; the cause is outside this repo\\n'\n")


def _prior_round(repo: Path, push: bool, marker: bool = True) -> str:
    """A prior implement round: one commit on the item branch, ahead of main.

    marker=True mirrors the real stage's commit shape (second -m carries the
    pipeline marker "board-pilot <ITEM_ID>"); marker=False is a FOREIGN commit —
    work the pipeline never minted."""
    _git(repo, "switch", "-c", BRANCH)
    (repo / "prior.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    if marker:
        _git(repo, "commit", "-m", "prior round",
             "-m", "board-pilot ITEM1 — https://github.com/example/repo/issues/120")
    else:
        _git(repo, "commit", "-m", "prior round")
    sha = _git(repo, "rev-parse", "HEAD")
    if push:
        _git(repo, "push", "-u", "origin", BRANCH)
    # Back on main: the stage itself must find and reuse the branch.
    _git(repo, "switch", "main")
    return sha


def _rework_env(env: dict, tmp_path: Path) -> dict:
    """Round 1, with the note a real reject edge would carry."""
    note = tmp_path / "note.txt"
    note.write_text("verify failed: No module named pytest\n")
    return {**env, "BOUNCES": "1", "REJECTION_NOTE_FILE": str(note)}


# ---------------------------------------------------------------- the two arms
def test_empty_diff_with_branch_ahead_of_base_is_success(env, repo, tmp_path):
    """The arm both live runs died on. Prior-round work makes the branch ahead of
    base; a do-nothing round on top of it is convergence, not failure — the change
    the item asked for already exists on the branch."""
    prior_sha = _prior_round(repo, push=True)
    claude = _noop_claude(tmp_path / "bin")

    r = _run({**_rework_env(env, tmp_path), "BP_CLAUDE": str(claude)}, repo)

    assert r.returncode == 0, f"a no-op round on an ahead branch must succeed: {r.stderr}"
    assert "no new change" in r.stdout, "the stage must say WHY an empty diff passed"
    assert "1 commit" in r.stdout, "the message must carry the evidence: the prior-round count"
    # Nothing new: no commit was minted on top of the prior round's work.
    assert _git(repo, "rev-parse", BRANCH) == prior_sha, "an empty round must not commit"
    remote = _git(repo, "ls-remote", "--heads", "origin", BRANCH)
    assert remote.split()[0] == prior_sha, (
        "the ahead arm's idempotent push must not move an already-current remote"
    )


def test_empty_diff_ahead_pushes_a_missing_remote_branch(env, repo, tmp_path):
    """The push half: round 1 committed, its push failed (set -e killed the stage
    after the commit), so the remote branch never came to exist. The retry round
    no-ops — and the ahead arm must publish the branch before exiting 0, or verify
    greens a LOCAL tree and pr.sh opens the PR on a branch origin never got."""
    prior_sha = _prior_round(repo, push=False)
    claude = _noop_claude(tmp_path / "bin")

    r = _run({**_rework_env(env, tmp_path), "BP_CLAUDE": str(claude)}, repo)

    assert r.returncode == 0, r.stderr
    remote = _git(repo, "ls-remote", "--heads", "origin", BRANCH)
    assert remote != "", "the ahead arm must push — origin never got the prior round's commit"
    assert remote.split()[0] == prior_sha, "remote SHA must equal the local SHA after the heal"


def test_empty_diff_ahead_heals_a_stale_remote(env, repo, tmp_path):
    """Same defect, stale variant: round 1 pushed C1, round 2 committed C2 and its
    push failed. The remote branch exists but is behind — the ahead arm's push must
    bring it up to the local SHA."""
    _prior_round(repo, push=True)
    _git(repo, "switch", BRANCH)
    (repo / "prior2.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "prior round 2",
         "-m", "board-pilot ITEM1 — https://github.com/example/repo/issues/120")
    local_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    claude = _noop_claude(tmp_path / "bin")

    r = _run({**_rework_env(env, tmp_path), "BP_CLAUDE": str(claude)}, repo)

    assert r.returncode == 0, r.stderr
    remote = _git(repo, "ls-remote", "--heads", "origin", BRANCH)
    assert remote.split()[0] == local_sha, "the ahead arm must heal a remote that is behind"


def test_foreign_commit_ahead_of_base_dies_naming_the_sha(env, repo, tmp_path):
    """A commit ahead of base WITHOUT the pipeline marker is not prior-round work —
    it is junk pushed by someone with repo access, and a do-nothing round must not
    ride it to the PR gate. The refusal must name the offending commit."""
    foreign_sha = _prior_round(repo, push=False, marker=False)
    claude = _noop_claude(tmp_path / "bin")

    r = _run({**_rework_env(env, tmp_path), "BP_CLAUDE": str(claude)}, repo)

    assert r.returncode != 0, "an unmarked commit ahead of base must fail the stage"
    assert "foreign commit" in r.stderr, "the refusal did not come from the marker guard"
    assert foreign_sha in r.stderr, "the refusal must name the offending sha"
    assert _git(repo, "ls-remote", "--heads", "origin", BRANCH) == "", (
        "a refused round must not have pushed the foreign commit"
    )


def test_foreign_commit_dies_even_when_the_model_writes_a_change(env, repo, tmp_path):
    """The sweep guards the PRODUCTIVE path too, and runs BEFORE the model. A
    poisoned branch must not ride into the PR merely because this round produced a
    real diff — and a stage that will refuse anyway must not pay for a model run
    first. The stub records whether it was ever invoked to pin that ordering."""
    foreign_sha = _prior_round(repo, push=False, marker=False)
    ran = tmp_path / "model-ran"
    claude = _stub(
        tmp_path / "bin",
        "claude",
        f"touch {ran}\nprintf 'y = 2\\n' > added.py\nprintf 'wrote the change\\n'\n",
    )

    r = _run({**_rework_env(env, tmp_path), "BP_CLAUDE": str(claude)}, repo)

    assert r.returncode != 0, "a poisoned branch must fail even on a productive round"
    assert "foreign commit" in r.stderr and foreign_sha in r.stderr
    assert not ran.exists(), (
        "the sweep must run BEFORE the model — a refused branch must not cost a paid run"
    )
    assert _git(repo, "ls-remote", "--heads", "origin", BRANCH) == "", (
        "a refused round must not have pushed anything"
    )


def test_empty_diff_on_fresh_branch_still_dies(env, repo, tmp_path):
    """The guard's original arm, unchanged: on a branch at base, an empty diff means
    the stage did not implement, and passing it forward would green a suite that
    tested nothing new and open a PR with no change in it."""
    claude = _noop_claude(tmp_path / "bin")

    r = _run({**env, "BP_CLAUDE": str(claude)}, repo)

    assert r.returncode != 0, "a do-nothing agent on a fresh branch must still fail"
    # Pin the reason: a bare `!= 0` is satisfied by rc=127 from a missing script.
    assert "empty diff" in r.stderr, "the refusal did not come from the empty-diff guard"
    assert _git(repo, "ls-remote", "--heads", "origin", BRANCH) == "", (
        "a failed round must not have pushed the branch"
    )


def test_nonempty_diff_still_commits_and_pushes(env, repo, tmp_path):
    """The negative control: the two-armed guard must not have touched the real
    path. A round that writes a change still commits it and publishes the branch."""
    claude = _stub(tmp_path / "bin", "claude", "printf 'y = 2\\n' > added.py\nprintf 'wrote the change\\n'\n")

    r = _run({**env, "BP_CLAUDE": str(claude)}, repo)

    assert r.returncode == 0, r.stderr
    local_sha = _git(repo, "rev-parse", BRANCH)
    remote = _git(repo, "ls-remote", "--heads", "origin", BRANCH)
    assert remote.split()[0] == local_sha, "the change was not pushed"
    assert env["ITEM_TITLE"] in _git(repo, "log", "-1", "--format=%s", BRANCH)


def test_the_ahead_check_relies_on_the_branch_helpers_one_fetch(env):
    """bp_ensure_branch already fetches origin/<base> before the guard can run; a
    second fetch in the stage would be a drifting copy of that guarantee — and the
    base must come from bp_base_branch, never a hardcoded branch name."""
    code = _code("implement.sh")
    assert "git fetch" not in code, "the fetch belongs to bp_ensure_branch, not the stage"
    assert "bp_base_branch" in code, "the ahead-check must resolve base like the rest of the chain"
