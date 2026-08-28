"""Recipients: a reference is only a reference if something resolves it.

`response.recipients` names a mandant slug and a person key, deliberately,
because a declaration is a tracked file and a plaintext address in it travels
with the scope router. The schema enforced that SHAPE and nothing ever looked
the reference up, which model.py said about itself in a comment: "no check that
the slug exists". So a typo in a slug passed `validate`, passed `provision`, and
produced a run that reports to nobody, with the declaration still stating who
was meant to be told.

Measured on this instance the day this was written: 25 recipient entries, 23 of
them carrying a person, and none broken. An open gate nobody had fallen through
yet, which is the only good moment to close one.

THE THIRD ANSWER is what makes this shippable. A checkout may hold no mandants
at all: the OSS upstream ships `_schema.yaml` and `_template.yaml` and not one
instance file. A resolver with two answers would call every reference there
unknown and refuse every declaration in the repository. So absence of the whole
directory is its own verdict, it is not an error, and it says so.
"""

from __future__ import annotations

import unittest

from tests.conftest import DERIVED, MachineGuard, mod

recipients = mod("engine.recipients")
model = mod("engine.model")

MANDANT = """\
schema_version: 1
scope: user
id: team
type: company
display_name: A Team
persons:
  - id: first_person
    display_name: First Person
  - id: second_person
    display_name: Second Person
"""


class ARecipientIsResolvedOrTheDeclarationSaysSo(MachineGuard):

    def repo(self, *, mandants=("team",), body=MANDANT, make_dir=True):
        root = self.tmpdir()
        if make_dir:
            folder = root / "identity" / "mandants"
            folder.mkdir(parents=True, exist_ok=True)
            # The companions ship everywhere and are never an instance.
            (folder / "_schema.yaml").write_text("# schema\n", encoding="utf-8")
            (folder / "_template.yaml").write_text("# template\n", encoding="utf-8")
            for slug in mandants:
                (folder / f"{slug}.yaml").write_text(body, encoding="utf-8")
        return root

    def load(self, name="twice-daily-report"):
        return model.load_declaration(DERIVED / f"{name}.yaml")

    def resolve(self, root, name="twice-daily-report"):
        return recipients.resolve(self.load(name), root)

    # ── the happy answer ────────────────────────────────────────────────────
    def test_a_declared_pair_that_exists_resolves(self):
        found = self.resolve(self.repo())
        self.assertTrue(found, "the fixture declares recipients and none came back")
        for one in found:
            self.assertEqual(one.state, recipients.RESOLVED,
                             f"{one.mandant}/{one.person} did not resolve: {one.detail}")

    def test_one_result_per_recipient_and_never_a_joined_string(self):
        # Derived data carries its own measure. Joined into one string, the
        # first person whose name holds a comma silently becomes two people.
        found = self.resolve(self.repo())
        self.assertIsInstance(found, list)
        self.assertEqual(len(found), 2,
                         "the declaration names two recipients and the answer "
                         f"has {len(found)} entries")

    # ── the two ways a reference is wrong ───────────────────────────────────
    def test_an_unknown_mandant_is_a_finding_that_names_the_slug(self):
        found = self.resolve(self.repo(mandants=("other",)))
        states = {one.state for one in found}
        self.assertIn(recipients.MANDANT_UNKNOWN, states)
        self.assertIn("team", " ".join(one.detail for one in found),
                      "the answer does not name the slug that could not be found")

    def test_a_person_the_mandant_does_not_list_is_its_own_answer(self):
        body = MANDANT.replace("  - id: second_person\n    display_name: Second Person\n", "")
        found = self.resolve(self.repo(body=body))
        states = [one.state for one in found]
        self.assertIn(recipients.RESOLVED, states,
                      "the person that IS listed stopped resolving")
        self.assertIn(recipients.PERSON_UNKNOWN, states)
        detail = " ".join(one.detail for one in found)
        self.assertIn("second_person", detail)
        self.assertIn("team", detail,
                      "a missing person has to name the mandant it was looked "
                      "for in, or the reader has two files to open")

    # ── the answer that makes this shippable ────────────────────────────────
    def test_a_checkout_without_mandants_cannot_verify_and_says_so(self):
        found = self.resolve(self.repo(make_dir=False))
        self.assertTrue(found)
        for one in found:
            self.assertEqual(
                one.state, recipients.NOT_VERIFIABLE,
                "a checkout that ships no mandants called the reference wrong. "
                "The OSS upstream is exactly that checkout, so this would refuse "
                "every declaration in the repository")

    def test_a_directory_holding_only_companions_is_also_not_verifiable(self):
        found = self.resolve(self.repo(mandants=()))
        for one in found:
            self.assertEqual(one.state, recipients.NOT_VERIFIABLE,
                             "the `_`-prefixed companions were counted as "
                             "instances, so a fresh clone looks configured")

    # ── what the command line does with it ──────────────────────────────────
    def test_findings_are_raised_for_the_wrong_ones_only(self):
        clean = recipients.findings_for([self.load()], self.repo())
        self.assertEqual(list(clean), [],
                         f"a resolvable declaration produced findings: {clean}")
        broken = recipients.findings_for([self.load()], self.repo(mandants=("other",)))
        self.assertTrue(broken, "an unknown mandant produced no finding")
        self.assertTrue(all(f.workload_id for f in broken))
        self.assertTrue(all(f.hint for f in broken),
                        "a finding without a repair is a complaint")

    def test_not_verifiable_never_becomes_a_finding(self):
        quiet = recipients.findings_for([self.load()], self.repo(make_dir=False))
        self.assertEqual(list(quiet), [],
                         "an unverifiable reference was reported as an error, "
                         "which turns every fresh clone red")

    def test_a_declaration_without_recipients_is_silent(self):
        self.assertEqual(recipients.resolve(self.load("silent-by-choice"), self.repo()), [])


if __name__ == "__main__":
    unittest.main()
