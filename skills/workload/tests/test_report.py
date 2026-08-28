"""report: what a finding is, and what happens to something that is not one.

Two properties are measured here, and both were live defects.

A `Report` used to accept anything at all in `findings` and only fall over
later, inside `by_severity`, with an `AttributeError` about `str`. The caller
that did it was `provision`, whose `Outcome.findings` is a tuple of plain
sentences, so the traceback landed on the one command that changes machines.

And the second gate's verdicts had no way into a report at all. They were
printed as prose and dropped, which is how `validate --strict` came to exit 0
over a declaration the schema had refused.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.conftest import MachineGuard, mod

model = mod("engine.model")
report = mod("engine.report")


def Verdict(verdict, detail=""):
    """The shape `model.validate_with_schema` returns, without importing it.

    A local stand-in on purpose: this file measures what `report` does with an
    answer, not how the answer was obtained. A function rather than a class
    because every class in a test file has to stand under the machine guard,
    and a two field record is not a test case.
    """
    return SimpleNamespace(verdict=verdict, detail=detail)


class APlainSentenceIsNotAFinding(MachineGuard):
    """The traceback that stood where a report was promised."""

    def test_a_sentence_in_a_report_renders_instead_of_raising(self):
        # The exact call `cli._run_provision` makes. Before this it raised
        # AttributeError: 'str' object has no attribute 'severity'.
        rep = report.Report(findings=["dry run, nothing was touched"])
        rendered = report.render_table(rep)
        self.assertIn("dry run, nothing was touched", rendered)
        self.assertNotIn(report.CLEAN_LINE, rendered,
                         "a report carrying a sentence rendered as clean")

    def test_by_severity_answers_over_sentences(self):
        rep = report.Report(findings=["one", "two"])
        self.assertEqual([f.detail for f in rep.by_severity()], ["one", "two"])

    def test_json_renders_over_sentences_too(self):
        # The other of the two ways a report is shown. It reads the same
        # attributes, so it broke the same way and nothing measured it.
        import json

        payload = json.loads(report.render_json(report.Report(findings=["a note"])))
        self.assertEqual(payload["findings"][0]["detail"], "a note")

    def test_a_sentence_never_decides_an_exit_code(self):
        # It carries no severity claim, so inventing a loud one here would
        # decide an exit code from prose. `provision` decides from the live
        # verify instead, and that separation is the point.
        self.assertEqual(report.Report(findings=["a note"]).exit_code, 0)

    def test_a_real_finding_passes_through_untouched(self):
        finding = report.Finding(workload_id="x", severity=model.Severity.high,
                                 detail="gone", hint="provision it")
        rep = report.Report(findings=[finding])
        self.assertIs(rep.findings[0], finding)
        self.assertEqual(rep.exit_code, 1)

    def test_notes_carry_the_id_they_are_given(self):
        made = report.notes(("a note",), workload_id="calendar-export")
        self.assertEqual(made[0].workload_id, "calendar-export")
        self.assertIn("calendar-export", report.render_table(report.Report(findings=made)))


class TheSchemaVerdictBecomesAFinding(MachineGuard):
    """The second gate's answers, as report input rather than as prose."""

    def finding(self, name, detail=""):
        return report.finding_for_schema_verdict("calendar-export", Verdict(name, detail),
                                                 source="calendar-export.yaml")

    def test_valid_is_the_only_silence(self):
        self.assertIsNone(self.finding("valid"))

    def test_a_refusal_is_loud_enough_to_change_the_exit_code(self):
        finding = self.finding("invalid", "does not match the pattern")
        self.assertIn(finding.severity, report.LOUD)
        self.assertEqual(report.Report(findings=[finding]).exit_code, 1)
        self.assertIn("does not match the pattern", finding.detail)

    def test_an_absent_validator_is_loud_too(self):
        # "a check nobody ran is not a green check". While this answer was only
        # printed, --strict on a machine without check-jsonschema -- the normal
        # case on a fresh clone -- was a switch with no effect.
        finding = self.finding("schema_validator_absent", "check-jsonschema is not on PATH")
        self.assertIn(finding.severity, report.LOUD)
        self.assertEqual(report.Report(findings=[finding]).exit_code, 1)

    def test_absent_and_refused_do_not_read_the_same(self):
        # One says the declaration is wrong, the other says nobody looked.
        # Collapsing them would report a machine without a tool as a bad file.
        absent = self.finding("schema_validator_absent", "check-jsonschema is not on PATH")
        refused = self.finding("invalid", "does not match the pattern")
        self.assertNotEqual(absent.state_value, refused.state_value)

    def test_a_missing_contract_does_not_read_as_a_refused_declaration(self):
        # Three different facts, three different sentences: the file is wrong,
        # nobody looked, there is nothing to look WITH. Collapsing the third into
        # the first reported a repository without its contract as a repository
        # full of broken declarations.
        missing = self.finding("schema_missing", "no declaration contract at /x/_schema.yaml")
        refused = self.finding("invalid", "does not match the pattern")
        absent = self.finding("schema_validator_absent", "check-jsonschema is not on PATH")
        self.assertIn(missing.severity, report.LOUD)
        self.assertNotEqual(missing.state_value, refused.state_value)
        self.assertNotIn("refused", missing.detail)
        self.assertNotEqual(missing.detail, absent.detail)

    def test_an_answer_this_skill_does_not_know_is_not_a_pass(self):
        # A verdict name that was never mapped must not fall through to silence:
        # a `.get` with a quiet default is how an unknown answer becomes green.
        finding = self.finding("something_new_the_tool_started_saying")
        self.assertIsNotNone(finding)
        self.assertIn(finding.severity, report.LOUD)
        self.assertIn("something_new_the_tool_started_saying", finding.detail)

    def test_every_non_valid_verdict_is_loud(self):
        # The property, not the table: whatever the map grows to, only `valid`
        # may be silence.
        for name in tuple(report.SCHEMA_VERDICTS) + ("invented",):
            with self.subTest(verdict=name):
                finding = self.finding(name, "said something")
                self.assertIsNotNone(finding, f"{name} produced no finding")
                self.assertIn(finding.severity, report.LOUD,
                              f"{name} produced a finding that cannot change an exit code")

    def test_a_multiline_answer_stays_one_row(self):
        # check-jsonschema answers in several lines. One line per finding is the
        # surface contract, and a row that silently breaks the column is how a
        # report stops being greppable.
        finding = self.finding("invalid", "errors were encountered.\n  file.yaml::$.x: nope")
        self.assertNotIn("\n", finding.detail)
        self.assertIn("file.yaml::$.x: nope", finding.detail)
