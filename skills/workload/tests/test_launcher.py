"""workload.sh: which interpreter actually runs this skill.

Written after the skill turned out not to start at all on the machine it is
meant to watch. There, `python3` on a non interactive PATH resolves to the
system one, which has no `yaml`, while a usable interpreter sits right beside
it. The failure was a bare ModuleNotFoundError from inside an import chain,
which says nothing about the actual problem.

The repository already carries this scar once: `/usr/bin/python3` is a
forwarder that resolves differently per machine, and a versioned path is
deleted by the upgrade that creates its successor. So the launcher may neither
trust the first `python3` it finds nor hardcode one.
"""

from __future__ import annotations

import os
import subprocess
import unittest

from tests.conftest import MachineGuard, SKILL_DIR

LAUNCHER = SKILL_DIR / "workload.sh"


class TheLauncherPicksAnInterpreterThatCanActuallyRunIt(MachineGuard):

    def fake_python(self, folder, body="exit 1"):
        """A `python3` on PATH that cannot run this skill."""
        folder.mkdir(parents=True, exist_ok=True)
        fake = folder / "python3"
        fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def launch(self, *argv, path=None, env=None):
        run_env = dict(os.environ)
        if path is not None:
            run_env["PATH"] = path
        run_env.update(env or {})
        return subprocess.run([str(LAUNCHER), *argv], capture_output=True,
                              text=True, timeout=120, env=run_env)

    def test_it_works_at_all_on_this_machine(self):
        done = self.launch("--help")
        self.assertEqual(done.returncode, 0,
                         f"the control for everything below:\n{done.stderr}")

    def test_an_interpreter_that_cannot_run_the_skill_is_passed_over(self):
        broken = self.tmpdir() / "bin"
        self.fake_python(broken)
        done = self.launch("--help", path=f"{broken}:{os.environ.get('PATH', '')}")
        self.assertEqual(
            done.returncode, 0,
            f"the first `python3` on PATH could not run the skill and the "
            f"launcher gave up instead of looking further. That is exactly how "
            f"this skill failed to start on the machine it watches:\n"
            f"{done.stderr}")

    def test_a_named_interpreter_that_cannot_run_it_says_what_is_missing(self):
        """PATH cannot be emptied for this: the absolute candidates exist anyway.

        So the case is driven through the one input that names an interpreter
        outright, which is also the likelier real mistake: somebody points
        BRIDGE_PYTHON at the wrong one and gets a bare ModuleNotFoundError out
        of an import chain, which sends them into the wrong file entirely.
        """
        broken = self.fake_python(self.tmpdir() / "bin")
        done = self.launch("--help", env={"BRIDGE_PYTHON": str(broken)})
        self.assertNotEqual(done.returncode, 0, "it must not pretend to work")
        said = (done.stderr + done.stdout).lower()
        self.assertIn(
            "python", said,
            f"the failure has to name the problem:\n{done.stderr}")
        self.assertIn(
            "yaml", said,
            "it has to say WHAT is missing, or the next person installs the "
            "wrong thing")

    def test_a_named_interpreter_is_the_one_that_actually_runs(self):
        """Not just that it fails when broken: that it is USED when it works."""
        folder = self.tmpdir() / "bin"
        marker = self.tmpdir() / "was-used"
        real = subprocess.run(["command", "-v", "python3"], capture_output=True,
                              text=True, shell=False, executable="/bin/sh",
                              input="") if False else None
        import shutil
        real_python = shutil.which("python3")
        self.assertTrue(real_python, "no python3 on PATH to build the wrapper from")
        wrapper = self.fake_python(
            folder, body=f'echo used > {marker}\nexec {real_python} "$@"')
        done = self.launch("--help", env={"BRIDGE_PYTHON": str(wrapper)})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(
            marker.exists(),
            "the interpreter named outright was not the one that ran, so a "
            "machine with an unusual layout has no way to say what to use")
