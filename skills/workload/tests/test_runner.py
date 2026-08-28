"""The tally: the runner's own verdict, which nothing else in this suite reads.

`run-tests.sh` prints one line per case and then a sentence that everyone treats
as the answer. That sentence is a claim about the run, so it needs a test like
any other claim, and it never had one.

WHAT IT LET THROUGH. total was ok + bad + skipped, and the verdict was green as
soon as bad was zero. In a detached copy `./run-tests.sh scaffold` therefore
printed "0/2 green" and exited 0 with nothing having run at all. That is the
worst possible place for it: the mutation battery works in exactly such a copy,
so a needle whose named test can only skip there would have been scored red
without a single assertion having executed.

The tally lives in `scripts/tests/tally.awk` so it can be fed a transcript here
instead of a whole suite run. Feeding it is the point: these cases hand it
transcripts it would otherwise only meet by accident.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest

from tests.conftest import SKILL_DIR, MachineGuard

TALLY = SKILL_DIR / "scripts" / "tests" / "tally.awk"
RUNNER = SKILL_DIR / "run-tests.sh"
MUTATE = SKILL_DIR / "scripts" / "tests" / "mutate.py"


def mutate_module():
    """The battery runner, loaded by path: it is a script and not a package."""
    spec = importlib.util.spec_from_file_location("_workload_mutate_under_test", MUTATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def line(case: str, verdict: str) -> str:
    """One verbose unittest line, in the shape the runner actually parses."""
    return f"{case.rsplit('.', 1)[-1]} ({case}) ... {verdict}"


def documented(case: str, doc: str, verdict: str) -> tuple[str, str]:
    """The TWO lines unittest prints when the METHOD carries a docstring.

    The identifier goes on its own line and the verdict lands on the next one,
    behind the docstring's first line. Nothing about the second line says which
    case it belongs to, which is the whole difficulty.
    """
    return (f"{case.rsplit('.', 1)[-1]} ({case})", f"{doc} ... {verdict}")


class TheTally(MachineGuard):
    """What the runner says about a run, and whether it may say green."""

    def tally(self, *lines: str) -> subprocess.CompletedProcess:
        transcript = "".join(f"{one}\n" for one in lines)
        return subprocess.run(["awk", "-f", str(TALLY)],
                              input=transcript, capture_output=True, text=True)

    def verdict(self, done: subprocess.CompletedProcess) -> str:
        body = [one for one in done.stdout.splitlines() if one.strip()]
        self.assertTrue(body, "the tally printed nothing at all")
        return body[-1]

    # ------------------------------------------------------------------
    # The one that was wrong
    # ------------------------------------------------------------------

    def test_a_run_in_which_everything_was_skipped_is_never_green(self):
        done = self.tally(
            line("tests.test_model.Scaffolding.test_one", "skipped 'needs the repo'"),
            line("tests.test_model.Scaffolding.test_two", "skipped 'needs the repo'"),
        )
        verdict = self.verdict(done)
        self.assertNotIn("green", verdict,
                         "nothing ran, and a tally that calls that green is the same "
                         "failure as a check nobody ran: " + verdict)
        self.assertEqual(done.returncode, 1,
                         "an all-skipped run exited 0, so a caller that only reads the "
                         "exit code sees a pass over nothing")

    # ------------------------------------------------------------------
    # The controls that give that one its meaning
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # A case that carries its own reason
    # ------------------------------------------------------------------

    def test_a_documented_case_is_named_by_its_identifier_not_its_prose(self):
        done = self.tally(*documented(
            "tests.test_reconcile.A.test_one",
            "The sentence next to it must not claim what it never looked at.", "ok"))
        self.assertIn(
            "test_reconcile.A.test_one", done.stdout,
            f"a case whose method has a docstring was listed under a fragment of "
            f"that prose, so it cannot be found by name in a transcript: "
            f"{done.stdout!r}")

    def test_two_documented_cases_are_two_cases_even_with_the_same_opening_words(self):
        done = self.tally(
            *documented("tests.test_reconcile.A.test_one",
                        "The sentence says one thing.", "ok"),
            *documented("tests.test_reconcile.A.test_two",
                        "The sentence says another.", "ok"))
        self.assertEqual(
            self.verdict(done), "2/2 green",
            "identity was taken from the second WORD of the docstring, so two "
            "cases opening alike collapsed into one and the tally under-counted")

    def test_a_documented_failure_hiding_behind_a_documented_pass_is_still_red(self):
        """The one that makes this worth fixing rather than tidying."""
        done = self.tally(
            *documented("tests.test_reconcile.A.test_one",
                        "The sentence says one thing.", "ok"),
            *documented("tests.test_reconcile.A.test_two",
                        "The sentence says another.", "FAIL"))
        self.assertEqual(
            done.returncode, 1,
            "a FAIL was dropped as a duplicate of an earlier PASS because both "
            "cases opened their docstring with the same two words, and the runner "
            "that issues every other proof in this skill exited 0 over it")
        self.assertIn("FAIL", done.stdout)

    def test_a_run_where_everything_passed_is_green_and_says_so(self):
        done = self.tally(
            line("tests.test_model.A.test_one", "ok"),
            line("tests.test_model.A.test_two", "ok"),
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.verdict(done), "2/2 green")

    def test_skips_alongside_real_passes_stay_green_but_are_counted_out_loud(self):
        # Three cases skip by design outside the repository. A run that has them
        # AND real passes is still a run; hiding the skips inside the total is
        # what made "0/2 green" possible in the first place.
        done = self.tally(
            line("tests.test_model.A.test_one", "ok"),
            line("tests.test_model.A.test_two", "skipped 'needs the repo'"),
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.verdict(done), "1/2 green, 1 skipped")

    def test_a_single_failure_is_red(self):
        done = self.tally(
            line("tests.test_model.A.test_one", "ok"),
            line("tests.test_model.A.test_two", "FAIL"),
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAILED", self.verdict(done))

    def test_an_error_counts_as_a_failure_and_not_as_a_skip(self):
        done = self.tally(line("tests.test_model.A.test_one", "ERROR"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAILED", self.verdict(done))

    def test_an_empty_transcript_is_red(self):
        done = self.tally()
        self.assertEqual(done.returncode, 1)
        self.assertIn("collected", self.verdict(done))

    def test_a_failing_subtest_is_counted_once_and_not_once_per_subtest(self):
        # A case with failing subtests prints one verbose line per subtest. The
        # first verdict per identifier is the one that counts, or one case with
        # forty subtests would outvote the rest of the file.
        case = "tests.test_model.A.test_one"
        done = self.tally(line(case, "FAIL"), line(case, "FAIL"), line(case, "FAIL"))
        self.assertEqual(self.verdict(done), "1 of 1 FAILED")

    # ------------------------------------------------------------------
    # The wiring, so the rule above is the one the runner really applies
    # ------------------------------------------------------------------

    def test_the_runner_really_uses_this_tally(self):
        # A pattern that matches nothing collects nothing. The answer has to come
        # from the file tested above, so this is the end-to-end proof that
        # run-tests.sh has not grown a second verdict of its own.
        done = subprocess.run([str(RUNNER), "zzz_no_such_case_zzz"],
                              cwd=str(SKILL_DIR), capture_output=True, text=True,
                              timeout=300)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("no test cases were collected", done.stdout)


class TheMutationVerdict(MachineGuard):
    """When a red counts as a proof, and when it is only a broken address.

    The battery asked one question of every needle: did the named test exit
    non-zero. It does that for a second reason as well. `python -m unittest
    <name>` exits 1 when the NAME cannot be loaded at all -- a renamed class, a
    method nobody ever wrote, a module that stopped importing -- and reports it
    as an ERROR under `unittest.loader._FailedTest`, one line away from a real
    failure and with the same exit code.

    WHAT IT LET THROUGH. Two needles in `tests/mutations.py` named methods that
    do not exist on the class they name. Both were scored red. Neither had ever
    executed a line of the behaviour it claims to guard, and the mutation they
    apply -- the guard script taking the id as it comes -- turns the whole suite
    green when it is applied for real. A false red is worse than a survivor: a
    survivor is printed.

    The negative control matters as much as the case: an exception RAISED BY THE
    CODE under test is exactly how several needles here bite, so `errors=1` on
    its own may never be rejected.
    """

    def verdict(self, returncode: int, output: str) -> str:
        return mutate_module().verdict_of(returncode, output)

    def transcript(self, head: str, tail: str, ran: int = 1) -> str:
        return (f"{head}\n{'=' * 70}\n{tail}\n\nRan {ran} test in 0.001s\n\n"
                f"FAILED (errors=1)\n")

    def test_a_case_that_ran_and_failed_is_the_proof(self):
        out = self.transcript("F", "FAIL: test_x (tests.test_a.B.test_x)\n"
                                   "AssertionError: the gate let it through")
        self.assertEqual(self.verdict(1, out), "red")

    def test_a_red_that_only_says_the_test_could_not_be_loaded_is_no_proof(self):
        out = self.transcript(
            "E", "ERROR: tests.test_backends (unittest.loader._FailedTest."
                 "tests.test_backends)\nAttributeError: type object 'AClass' has "
                 "no attribute 'test_a_method_nobody_wrote'")
        verdict = self.verdict(1, out)
        self.assertNotEqual(verdict, "red",
                            "a needle whose test could not be loaded was scored as a "
                            "proof: " + verdict)
        self.assertIn("LOADED", verdict,
                      "the reason does not say what went wrong, so nobody can fix it")

    def test_an_exception_the_code_under_test_raised_is_still_a_proof(self):
        # The negative control. Several needles bite by making the code raise
        # where the contract promises a report; rejecting every `errors=1` would
        # turn those into survivors and hide real teeth.
        out = self.transcript(
            "E", "ERROR: test_x (tests.test_report.B.test_x)\n"
                 "AttributeError: 'str' object has no attribute 'severity'")
        self.assertEqual(self.verdict(1, out), "red")

    def test_a_green_run_is_never_a_proof(self):
        self.assertNotEqual(self.verdict(0, "Ran 1 test in 0.001s\n\nOK\n"), "red")

    def test_a_run_in_which_no_case_ran_is_never_a_proof(self):
        self.assertNotEqual(
            self.verdict(1, "Ran 0 tests in 0.000s\n\nNO TESTS RAN\n"), "red")
        self.assertNotEqual(self.verdict(1, "nothing that looks like a run\n"), "red")

    # ------------------------------------------------------------------
    # The wiring, end to end, so the rule above is the one the battery applies
    # ------------------------------------------------------------------

    def battery_over(self, test_name: str) -> subprocess.CompletedProcess:
        """A throwaway skill with ONE needle, driven by the real mutate.py.

        Small enough to run twice in a test: one source file, one test file, one
        entry in the battery. The runner under test is the real one, copied in.
        """
        skill = self.tmpdir() / "skill"
        (skill / "engine").mkdir(parents=True)
        (skill / "scripts" / "tests").mkdir(parents=True)
        (skill / "tests").mkdir(parents=True)
        (skill / "engine" / "__init__.py").write_text("", encoding="utf-8")
        (skill / "engine" / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")
        (skill / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (skill / "tests" / "test_thing.py").write_text(
            "import unittest\n"
            "from engine.thing import VALUE\n"
            "class TheValue(unittest.TestCase):\n"
            "    def test_the_value_is_one(self):\n"
            "        self.assertEqual(VALUE, 1)\n", encoding="utf-8")
        (skill / "tests" / "mutations.py").write_text(
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class Mutation:\n"
            "    name: str\n    file: str\n    search: str\n"
            "    replace: str\n    test: str\n    scar: str\n"
            "MUTATIONS = (Mutation(name='the-value-changes', file='engine/thing.py',\n"
            "                      search='VALUE = 1', replace='VALUE = 2',\n"
            f"                      test={test_name!r}, scar='the value drifted'),)\n",
            encoding="utf-8")
        (skill / "scripts" / "tests" / "mutate.py").write_text(
            MUTATE.read_text(encoding="utf-8"), encoding="utf-8")
        return subprocess.run([sys.executable, str(skill / "scripts" / "tests" / "mutate.py")],
                              capture_output=True, text=True, timeout=300)

    def test_the_battery_reports_a_needle_whose_test_does_not_exist(self):
        done = self.battery_over("tests.test_thing.TheValue.test_a_method_nobody_wrote")
        self.assertEqual(done.returncode, 1,
                         "a needle naming a method nobody wrote was scored as a proof:\n"
                         + done.stdout + done.stderr)
        self.assertIn("SURVIVED", done.stdout)
        self.assertIn("LOADED", done.stdout)

    def test_the_battery_still_scores_a_real_red_as_one(self):
        # The Gegenprobe of the case above: the same battery, the same mutation,
        # a test that exists. Without this, refusing everything would pass.
        done = self.battery_over("tests.test_thing.TheValue.test_the_value_is_one")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("1/1 mutations turned their test red", done.stdout)


if __name__ == "__main__":
    unittest.main()
