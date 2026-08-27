#!/usr/bin/env python3
"""Apply the mutation battery to a scratch copy and demand that each one goes red.

A green suite proves nothing on its own: it also stays green when the code it is
supposed to guard has been softened, unless somebody checks. This is that check.

For every entry in ``tests/mutations.py``:

1. copy the whole skill into a throwaway directory,
2. replace one literal in one engine source,
3. run ONLY the named test there,
4. demand that it FAILS.

The working tree is never modified, and a mutation whose anchor no longer exists
is a failure too: an anchor that has drifted means the mutation stopped applying
and the entry has been proving nothing since.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL))

from tests.mutations import MUTATIONS          # noqa: E402

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def apply_one(mutation, workspace: Path) -> str:
    target = workspace / mutation.file
    text = target.read_text(encoding="utf-8")
    hits = text.count(mutation.search)
    if hits != 1:
        return (f"the anchor appears {hits} times in {mutation.file}, so the "
                f"mutation applies to nothing and has been proving nothing")
    target.write_text(text.replace(mutation.search, mutation.replace), encoding="utf-8")
    return ""


def run_named_test(mutation, workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "unittest", mutation.test],
        cwd=workspace, capture_output=True, text=True, timeout=300,
    )


#: The verdict of a single needle, when it really is one.
RED = "red"


def verdict_of(returncode: int, output: str) -> str:
    """``RED``, or the sentence saying why this run proves nothing.

    A non-zero exit is NOT the proof, and reading it as one is how this battery
    scored two needles red that had never executed a line of the behaviour they
    name. `python -m unittest <name>` also exits 1 when the name cannot be
    loaded: a renamed class, a method nobody wrote, a module that stopped
    importing. unittest reports that under `unittest.loader._FailedTest`, one
    line away from a real failure and with the same exit code.

    So a red counts only when a case RAN and the run reported a verdict about
    its behaviour. Everything else is handed back as a survivor with the reason
    printed, because a needle that cannot be told apart from a broken import is
    a proof nobody ran.
    """
    if "unittest.loader._FailedTest" in output or "Failed to import test module" in output:
        return ("the named test could not be LOADED, so the red says nothing about "
                "the behaviour this needle claims to guard: check the class and "
                "method name, they are the address of the proof")
    ran = re.search(r"^Ran (\d+) test", output, re.M)
    if ran is None:
        return "the run reported no case at all, so nothing measured this mutation"
    if int(ran.group(1)) == 0:
        return "no test case ran, so nothing measured this mutation"
    if returncode == 0:
        return "the named test stayed green"
    return RED


def main() -> int:
    print(f"mutation battery: {len(MUTATIONS)} needle(s), each applied to a scratch copy\n")
    red = 0
    survivors = []
    for mutation in MUTATIONS:
        root = Path(tempfile.mkdtemp(prefix="workload-mutate-"))
        workspace = root / "workload"
        try:
            shutil.copytree(SKILL, workspace, ignore=IGNORE)
            trouble = apply_one(mutation, workspace)
            if trouble:
                print(f"  SURVIVED  {mutation.name}  ({trouble})")
                survivors.append(mutation)
                continue
            done = run_named_test(mutation, workspace)
            verdict = verdict_of(done.returncode, done.stdout + done.stderr)
            if verdict != RED:
                print(f"  SURVIVED  {mutation.name}")
                print(f"            {mutation.test}: {verdict}")
                print(f"            what it lets through: {mutation.scar}")
                survivors.append(mutation)
            else:
                print(f"  red       {mutation.name}")
                red += 1
        finally:
            shutil.rmtree(root, ignore_errors=True)

    print()
    if survivors:
        print(f"{len(survivors)} of {len(MUTATIONS)} mutations SURVIVED the suite")
        print("A suite that stays green under a softened guard is not a suite.")
        return 1
    print(f"{red}/{len(MUTATIONS)} mutations turned their test red")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
