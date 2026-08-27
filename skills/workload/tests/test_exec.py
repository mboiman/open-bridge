"""exec: the only subprocess path in the skill, and the three scars in it.

This is the one file that starts real processes. It has to. A mock cannot show
that a grandchild survived a kill, and "an exception was raised" is precisely
the assertion that misses the bug this module exists to prevent:
`subprocess.run(timeout=...)` DOES raise, kills the direct child, and then
blocks forever in the cleanup because a grandchild still holds the output pipe.
A suite that only checks for the exception is green on exactly that failure.

Everything started here is local and harmless (`/bin/sh` and `sleep`) and dies
under the test's own deadline. Nothing here touches a remote machine.
"""

from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from tests.conftest import FIXTURE_HOME, FakeHost, MachineGuard, SKILL_DIR, mod

exec_mod = mod("engine.exec")
errors = mod("engine.errors")
model = mod("engine.model")
config = mod("engine.config")

#: The child sleeps, and so does a grandchild it spawns. The grandchild
#: inherits stdout, which is what keeps the pipe open after the child dies.
GRANDCHILD_SCRIPT = """\
#!/bin/sh
sleep 300 &
printf 'child %s\\ngrandchild %s\\n' "$$" "$!" > "$1"
echo "started"
# exec, so the shell BECOMES the foreground sleeper and the pid written above
# stays the only child pid. Without it a fork would leave a third process that
# neither the test nor a naive cleanup knows about.
exec sleep 300
"""

READS_STDIN_SCRIPT = """\
#!/bin/sh
read line
echo "read returned $?"
"""

PRINTS_THEN_HANGS_SCRIPT = """\
#!/bin/sh
echo "partial stdout"
echo "partial stderr" >&2
exec sleep 300
"""

MARKER_SCRIPT = """\
#!/bin/sh
touch "$1"
"""


def live_group_members(pgid: int) -> list:
    """Every process in the group that is not already a reaped shell of itself."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="],
                         capture_output=True, text=True).stdout
    members = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, pg, stat = parts[0], parts[1], parts[2]
        if pg == str(pgid) and not stat.startswith("Z"):
            members.append(int(pid))
    return members


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return bool(live_group_members(os.getpgid(pid))) if False else True


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class RealProcessBase(MachineGuard):

    def setUp(self):
        super().setUp()
        self._doomed = []
        self.addCleanup(self._reap_everything)
        self.dir = self.tmpdir()

    def _reap_everything(self):
        for pid in self._doomed:
            for sig in (signal.SIGKILL,):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass

    def script(self, body: str, name: str = "probe.sh") -> Path:
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def pids_from(self, pidfile: Path, seconds: float = 5.0) -> tuple:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if pidfile.exists():
                text = pidfile.read_text(encoding="utf-8")
                if "grandchild" in text:
                    child = int(text.split("child ")[1].split("\n")[0])
                    grand = int(text.split("grandchild ")[1].split("\n")[0])
                    self._doomed.extend([child, grand])
                    return child, grand
            time.sleep(0.05)
        raise AssertionError(f"the helper script never wrote its pids to {pidfile}")


class TheDeadlineKillsTheProcessGroup(RealProcessBase):
    """Hard rules 1 and 2, proven against real processes.

    Rule 1: an expired deadline is a REPORTED error, never silence and never a
    synthetic return code somebody can ignore.
    Rule 2: the whole process group dies. Killing only the direct child leaves a
    grandchild holding the output pipe, and the cleanup then blocks forever.
    """

    def test_control_a_naive_deadline_leaves_the_grandchild_alive(self):
        # Needs no engine at all. This is half of the failure the module exists
        # to prevent: subprocess.run(timeout=...) DOES raise and DOES kill the
        # direct child, and the grandchild carries on holding the output pipe.
        # If this control ever goes green the other way round, the helper script
        # stopped producing a surviving grandchild and every process group
        # assertion in this file became decorative.
        pidfile = self.dir / "control-a.pids"
        script = self.script(GRANDCHILD_SCRIPT)

        seen = {}

        def naive():
            try:
                subprocess.run([str(script), str(pidfile)],
                               capture_output=True, text=True, timeout=2)
                seen["returned"] = True
            except subprocess.TimeoutExpired:
                seen["raised"] = True

        worker = threading.Thread(target=naive, daemon=True)
        worker.start()
        child, grand = self.pids_from(pidfile)
        worker.join(timeout=10)

        grandchild_survived = not wait_gone(grand, seconds=0.5)
        os.kill(grand, signal.SIGKILL)
        self.assertTrue(seen.get("raised"), "the naive call did not even reach its deadline")
        self.assertTrue(grandchild_survived,
                        f"grandchild {grand} died without a group kill, so this file no "
                        f"longer reproduces the failure it exists for")

    def test_control_b_cleanup_blocks_when_only_the_child_was_killed(self):
        # The other half, and the shape of the three and a half hour hang: the
        # child is dead, the grandchild still holds the write end of the pipe,
        # and communicate() waits for an EOF that never comes.
        pidfile = self.dir / "control-b.pids"
        script = self.script(GRANDCHILD_SCRIPT)

        proc = subprocess.Popen([str(script), str(pidfile)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True)
        self._doomed.append(proc.pid)
        child, grand = self.pids_from(pidfile)

        proc.kill()  # the direct child only, exactly what a naive cleanup does
        finished = threading.Event()

        def drain():
            try:
                proc.communicate()
            finally:
                finished.set()

        threading.Thread(target=drain, daemon=True).start()
        blocked = not finished.wait(timeout=4)

        os.kill(grand, signal.SIGKILL)
        finished.wait(timeout=5)

        self.assertTrue(blocked,
                        "communicate() returned although a grandchild still held the pipe, "
                        "so the control no longer demonstrates the hang")

    def test_an_expired_deadline_raises_and_does_not_block(self):
        pidfile = self.dir / "engine.pids"
        script = self.script(GRANDCHILD_SCRIPT)
        started = time.monotonic()
        with self.assertRaises(errors.StepTimeout) as ctx:
            exec_mod.run_argv([str(script), str(pidfile)], timeout_sec=2)
        elapsed = time.monotonic() - started
        self.assert_error(ctx, "step-timeout")
        self.assertLess(elapsed, 15,
                        "the call blocked after the deadline, which is the whole bug")

    def test_no_process_of_the_group_is_left_alive(self):
        pidfile = self.dir / "engine.pids"
        script = self.script(GRANDCHILD_SCRIPT)
        with self.assertRaises(errors.StepTimeout):
            exec_mod.run_argv([str(script), str(pidfile)], timeout_sec=2)
        child, grand = self.pids_from(pidfile, seconds=1.0)

        self.assertTrue(wait_gone(grand, seconds=5),
                        f"the grandchild {grand} outlived the deadline: only the direct "
                        f"child was killed")

        deadline = time.monotonic() + 5
        remaining = live_group_members(child)
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = live_group_members(child)
        self.assertEqual(remaining, [],
                         f"processes still alive in group {child}: {remaining}")

    def test_the_deadline_is_never_returned_as_a_code(self):
        # A synthetic 124 turns a hang into a value somebody can ignore.
        script = self.script(PRINTS_THEN_HANGS_SCRIPT)
        try:
            done = exec_mod.run_argv([str(script)], timeout_sec=2)
        except Exception as exc:
            self.assertEqual(getattr(exc, "code", None), "step-timeout")
        else:
            self.fail(f"an expired deadline returned {done!r} instead of raising")

    def test_the_partial_output_survives_the_timeout(self):
        script = self.script(PRINTS_THEN_HANGS_SCRIPT)
        with self.assertRaises(errors.StepTimeout) as ctx:
            exec_mod.run_argv([str(script)], timeout_sec=2)
        exc = ctx.exception
        self.assertEqual(tuple(exc.argv)[0], str(script))
        self.assertEqual(exc.timeout_sec, 2)
        self.assertIn("partial stdout", exc.partial_stdout)
        self.assertIn("partial stderr", exc.partial_stderr)

    def test_the_popen_call_itself_asks_for_a_new_session(self):
        # Parsed, not grepped.
        # A substring search over this file is satisfied by the module
        # docstring, which mentions ``start_new_session=True`` in prose. Turning
        # the real keyword to False therefore left the promise green while
        # removing the behaviour. This walks to the Popen call and reads the
        # constant that is actually passed.
        source = (SKILL_DIR / "engine" / "exec.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "Popen":
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            self.assertIn("start_new_session", keywords,
                          "the child inherits the caller's group, so there is none to kill")
            value = keywords["start_new_session"]
            self.assertIsInstance(value, ast.Constant)
            self.assertIs(value.value, True,
                          f"exec.py:{node.lineno} starts the child in the caller's "
                          f"process group")
            found.append(node.lineno)
        self.assertTrue(found, "no Popen call found, so this check proves nothing")
        self.assertNotIn("shell=True", source)
        self.assertIn("killpg", source)

    def test_the_child_really_lands_in_a_group_of_its_own(self):
        # The behavioural half, measured instead of killed.
        # The other group tests in this class prove the kill; they cannot prove
        # the SEPARATION, because with the separation gone the kill lands on the
        # test runner's own group and the suite dies without printing a verdict.
        # A dead runner is not a red test. This one only reads the group ids.
        marker = self.dir / "group.txt"
        script = self.script('#!/bin/sh\nps -o pgid= -p $$ | tr -d " " > "$1"\n')
        done = exec_mod.run_argv([str(script), str(marker)], timeout_sec=10)
        self.assertEqual(done.rc, 0)
        child_group = int(marker.read_text(encoding="utf-8").strip())
        self.assertNotEqual(
            child_group, os.getpgid(0),
            "the child shares the caller's process group, so killing that group "
            "would take the caller down and the deadline cannot kill it whole")


class EveryCallCarriesADeadline(RealProcessBase):
    """Rule 1, at the door instead of only in the docstring.

    ``proc.wait(timeout=None)`` waits forever. A caller that passed no deadline
    therefore did not fail, it hung, and a hang that produces a clean Completed
    at the end looks exactly like a run that finished. The ssh side has refused
    badly NESTED deadlines from the start; the deadline that is simply absent
    had no refusal at all, and every caller supplying one today is a habit, not
    a guarantee.

    Measured against a real process on purpose: an assertion about the argument
    would pass against a function that then still waits without a limit.
    """

    #: Long enough that a missing deadline is unmistakable, short enough that a
    #: regression costs seconds rather than a session.
    SLEEPER = ["/bin/sleep", "5"]

    def test_a_call_without_a_deadline_is_refused_before_anything_starts(self):
        started = time.monotonic()
        with self.assertRaises(errors.InvalidTimeout) as ctx:
            exec_mod.run_argv(self.SLEEPER, timeout_sec=None)
        self.assertLess(time.monotonic() - started, 1.0,
                        "the refusal came after the call had already been waited on")
        self.assertEqual(ctx.exception.code, "invalid-timeout")
        self.assertIn("sleep", str(ctx.exception),
                      "the refusal does not name the call it refused")

    def test_a_deadline_of_zero_or_less_is_refused_too(self):
        # Zero is not "no limit" and a negative number is not a limit at all;
        # both would reach proc.wait as something it cannot honour.
        for absent in (0, -1, "60"):
            with self.subTest(timeout_sec=absent):
                with self.assertRaises(errors.InvalidTimeout):
                    exec_mod.run_argv(self.SLEEPER, timeout_sec=absent)

    def test_a_deadline_that_can_never_expire_is_refused_too(self):
        # `inf` and `nan` are floats, and both walked past a `<= 0` test: inf is
        # larger than everything, nan compares False against everything. Either
        # one reached `proc.wait(timeout=...)` as a deadline that can never fire.
        # Measured against the sleeper on purpose: with such a value accepted the
        # call does not raise, it waits out the whole sleep and hands back a
        # Completed that reads exactly like a bounded run.
        for never in (float("inf"), float("nan"), float("-inf")):
            with self.subTest(timeout_sec=never):
                started = time.monotonic()
                with self.assertRaises(errors.InvalidTimeout) as ctx:
                    exec_mod.run_argv(self.SLEEPER, timeout_sec=never)
                self.assertLess(
                    time.monotonic() - started, 1.0,
                    "the sleeper was allowed to run its course, so this value was "
                    "not refused as a deadline, it was honoured as none at all")
                self.assertEqual(ctx.exception.code, "invalid-timeout")

    def test_a_real_deadline_is_still_honoured(self):
        # The control: the refusal must not have swallowed the normal path.
        done = exec_mod.run_argv(["/bin/echo", "ok"], timeout_sec=5)
        self.assertEqual(done.rc, 0)


class RunArgvBasics(RealProcessBase):

    def test_a_clean_call_returns_a_completed(self):
        done = exec_mod.run_argv(["/bin/echo", "ok"], timeout_sec=5)
        self.assertEqual(done.rc, 0)
        self.assertEqual(done.stdout.strip(), "ok")

    def test_a_nonzero_code_is_returned_not_raised(self):
        script = self.script("#!/bin/sh\nexit 3\n")
        done = exec_mod.run_argv([str(script)], timeout_sec=5)
        self.assertEqual(done.rc, 3)

    def test_stdin_is_closed_so_a_reader_does_not_hang(self):
        script = self.script(READS_STDIN_SCRIPT)
        done = exec_mod.run_argv([str(script)], timeout_sec=5)
        self.assertIn("read returned", done.stdout)

    def test_utf8_output_survives(self):
        script = self.script("#!/bin/sh\nprintf 'Grüße €\\n'\n")
        done = exec_mod.run_argv([str(script)], timeout_sec=5)
        self.assertIn("Grüße €", done.stdout)

    def test_the_environment_is_passed_through_when_given(self):
        script = self.script('#!/bin/sh\necho "$WORKLOAD_TEST_TOKEN"\n')
        done = exec_mod.run_argv([str(script)], timeout_sec=5,
                                 env={"WORKLOAD_TEST_TOKEN": "sentinel", "PATH": "/usr/bin:/bin"})
        self.assertIn("sentinel", done.stdout)


class RunStep(RealProcessBase):

    def step(self, argv, **kw):
        return model.Step(argv=tuple(argv), purpose=kw.pop("purpose", "a test step"), **kw)

    def local(self):
        return FakeHost(slug="local", platform="macos", is_local=True)

    def test_an_unexpected_code_raises_with_the_first_stderr_line(self):
        script = self.script("#!/bin/sh\necho 'the real reason' >&2\necho 'noise' >&2\nexit 2\n")
        with self.assertRaises(errors.StepFailed) as ctx:
            exec_mod.run_step(self.step([str(script)]), self.local(), default_timeout_sec=5)
        self.assert_error(ctx, "step-failed", "the real reason")

    def test_an_expected_code_is_not_a_failure(self):
        script = self.script("#!/bin/sh\nexit 1\n")
        done = exec_mod.run_step(self.step([str(script)], expect_rc=(0, 1)),
                                 self.local(), default_timeout_sec=5)
        self.assertEqual(done.rc, 1)

    def test_the_step_timeout_wins_over_the_default(self):
        script = self.script(PRINTS_THEN_HANGS_SCRIPT)
        started = time.monotonic()
        with self.assertRaises(errors.StepTimeout):
            exec_mod.run_step(self.step([str(script)], timeout_sec=1),
                              self.local(), default_timeout_sec=600)
        self.assertLess(time.monotonic() - started, 15)

    def test_an_elevated_step_is_never_executed_here(self):
        marker = self.dir / "elevation.marker"
        script = self.script(MARKER_SCRIPT, name="marker.sh")
        with self.assertRaises(errors.ElevationRequired) as ctx:
            exec_mod.run_step(self.step([str(script), str(marker)], requires_elevation=True),
                              self.local(), default_timeout_sec=5)
        self.assert_error(ctx, "elevation-required")
        self.assertFalse(marker.exists(),
                         "an elevated step was executed instead of being handed to a human")


class TheHostFactsAreReadOrTheRunStops(MachineGuard):
    """An empty answer is not an answer, and this one was taken as three.

    `probe_context` reads uid, home and zone off the machine because render must
    not guess them. It read them positionally out of stdout, padded the list with
    empty strings, and returned whatever fell out. A probe that produced NOTHING
    therefore produced a context of three empty strings, and the rest of the
    plan was built on it without a word: the launchd target became `gui//label`,
    every path became root anchored, and provision refused with a collision
    against a unit that does not exist.

    The return code did not help. The step is deliberately run with no expected
    rc, and the failure mode that actually happened exited ZERO.
    """

    def host(self):
        return FakeHost.from_fixture("host-a")

    def context_from(self, stdout, rc=0):
        cfg = config.load_config(SKILL_DIR)
        return exec_mod.probe_context(
            self.host(), cfg, timeout_sec=10,
            runner=lambda step, host, **kw: exec_mod.Completed(rc=rc, stdout=stdout))

    def test_an_empty_answer_is_refused(self):
        with self.assertRaises(Exception) as caught:
            self.context_from("")
        self.assertIn("uid", str(caught.exception).lower(),
                      "the refusal has to name what could not be read")

    def test_a_uid_that_is_not_a_number_is_refused(self):
        # `gui/<uid>` is built from it. A word there addresses no domain, and
        # the error it produces on the machine is `Bad request`, which reads
        # like a bug in the caller rather than a bad value.
        with self.assertRaises(Exception):
            self.context_from("nobody\n/home/x\nEurope/Berlin\n")

    def test_a_home_that_is_not_absolute_is_refused(self):
        with self.assertRaises(Exception):
            self.context_from("501\nrelative/home\nEurope/Berlin\n")

    def test_a_missing_zone_is_not_fatal(self):
        # Deliberately the odd one out. The zone has a legitimate fallback (the
        # host's declared one), the other two do not.
        ctx = self.context_from("501\n/opt/home/x\n\n")
        self.assertEqual(ctx.uid, "501")
        self.assertEqual(ctx.home, "/opt/home/x")

    def test_a_full_answer_passes(self):
        ctx = self.context_from("501\n/opt/home/x\nEurope/Berlin\n")
        self.assertEqual((ctx.uid, ctx.home, ctx.host_timezone),
                         ("501", "/opt/home/x", "Europe/Berlin"))


class SshArgv(MachineGuard):
    """Built here, executed nowhere. The guard makes sure of that."""

    def host(self):
        return FakeHost.from_fixture("host-a")

    def test_the_shape(self):
        argv = exec_mod.ssh_argv(self.host(), ["launchctl", "print", "gui/4242/x"],
                                 connect_timeout_sec=10)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-n", argv, "an ssh that inherits stdin hangs under a service manager")
        self.assertIn("BatchMode=yes", " ".join(argv))
        self.assertIn("ConnectTimeout=10", " ".join(argv))
        self.assertIn("opuser@host-a", " ".join(argv))

    def test_the_payload_is_quoted_not_concatenated(self):
        argv = exec_mod.ssh_argv(self.host(), ["/bin/echo", "two words; rm -rf /"],
                                 connect_timeout_sec=10)
        joined = " ".join(argv)
        self.assertNotIn("; rm -rf /", joined.replace("'; rm -rf /'", ""))

    def test_the_remote_shell_receives_the_argv_it_was_given(self):
        """ssh hands the remote login shell ONE string, and it splits it itself.

        That is the whole trap. Everything after the target is concatenated with
        spaces and parsed by the login shell over there, so the only property
        worth asserting is what that shell ends up with. The two cases above
        assert the SHAPE of the local argv and a substring of it, which is not
        the same thing and let this through.

        The case that broke: a step whose argv is ALREADY a shell invocation.
        `/bin/sh -c <script>` wrapped a second time gives the outer `sh -c` three
        words, and `sh -c` takes only the FIRST as its command string. It ran a
        bare `/bin/sh` with stdin closed, read nothing, and exited zero. Empty
        output, return code zero, no error anywhere: the host facts came back as
        three empty strings and the plan was built on them.
        """
        import shlex
        for original in (
            ["launchctl", "print", "gui/4242/x"],
            ["/bin/echo", "two words; rm -rf /"],
            ["/bin/sh", "-c", 'id -u; echo "$HOME"; readlink /etc/localtime'],
            ["/bin/sh", "-c", "printf '%s\\n' one two"],
        ):
            with self.subTest(argv=original):
                argv = exec_mod.ssh_argv(self.host(), original, connect_timeout_sec=10)
                sent = " ".join(argv[argv.index("--") + 1:])
                words = shlex.split(sent)
                self.assertEqual(
                    words[:2], ["/bin/sh", "-c"],
                    f"the remote shell splits {sent!r} into {words!r}")
                self.assertEqual(
                    len(words), 3,
                    "sh -c takes the FIRST word as the command string and turns "
                    f"the rest into positional parameters; it got {words!r}")
                self.assertEqual(
                    shlex.split(words[2]), original,
                    "the command string the remote sh runs must re-parse to the "
                    "argv this was called with, and nothing else")

    def test_a_nonstandard_port_is_carried(self):
        host = self.host()
        host.ssh_port = 2222
        self.assertIn("2222", " ".join(exec_mod.ssh_argv(host, ["/bin/true"],
                                                         connect_timeout_sec=10)))

    def test_a_connect_timeout_at_or_above_the_step_deadline_is_refused(self):
        # Otherwise the outer deadline is the only one that ever fires and the
        # connect phase is unbounded in practice.
        with self.assertRaises(errors.InvalidTimeout) as ctx:
            exec_mod.run_step(model.Step(argv=("/bin/true",), purpose="p", timeout_sec=5),
                              self.host(), default_timeout_sec=5, connect_timeout_sec=10)
        self.assert_error(ctx, "invalid-timeout")


class ProbeContext(MachineGuard):

    def test_it_asks_the_host_instead_of_guessing(self):
        from tests.conftest import RecordingRunner, completed_from

        runner = RecordingRunner()
        runner.add("", completed_from("probe-context.txt"))
        root = self.tmpdir()
        (root / "bridge-config.yaml").write_text("workloads: {}\n", encoding="utf-8")
        cfg = config.load_config(root)
        ctx = exec_mod.probe_context(FakeHost.from_fixture("host-a"), cfg,
                                     timeout_sec=10, runner=runner)
        self.assertEqual(ctx.uid, "4242")
        self.assertEqual(ctx.home, "/home/opuser")
        self.assertEqual(ctx.host_timezone, "Europe/Berlin")

    def test_it_is_one_cheap_read_only_call(self):
        from tests.conftest import RecordingRunner, completed_from

        runner = RecordingRunner()
        runner.add("", completed_from("probe-context.txt"))
        root = self.tmpdir()
        (root / "bridge-config.yaml").write_text("workloads: {}\n", encoding="utf-8")
        cfg = config.load_config(root)
        exec_mod.probe_context(FakeHost.from_fixture("host-a"), cfg,
                               timeout_sec=10, runner=runner)
        self.assertEqual(len(runner.calls), 1, runner.joined_calls)
        self.assert_no_mutation(runner)

    def test_the_uid_is_never_a_literal(self):
        source = (SKILL_DIR / "engine" / "exec.py").read_text(encoding="utf-8")
        self.assertNotIn("501", source)


if __name__ == "__main__":
    unittest.main()
