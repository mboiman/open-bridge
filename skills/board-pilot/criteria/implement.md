# Criteria — `implement` stage

The standard the implementer is held to. Edit this file to change **how code gets
written**; the record cites `implement.md@<sha>` on every implement line, including
each rework round, so a change to this file is visible in the trail, dated, and
attached to an outcome.

This file is read by a model and diffed by a human. Keep it plain prose and plain
rules.

## You have no shell — know your reach before you start

Your tools are Read, Glob, Grep, Write and Edit. **There is no Bash.** You cannot run
a generator, a build step, a package install, a test, or any command-line tool — this
is deliberate (edit permission plus a shell is a full escape hatch), and no amount of
retrying changes it.

The consequence: **you can produce source text, never a generated artifact.** A task
whose correct solution is "run script X and commit its output" cannot be completed
here — the first live task on this pipeline died exactly that way, on a plan that was
correct and an executor that could not run its one required command.

If the plan or the requirement needs execution to complete, do not fake it: never
hand-write a file that a generator is supposed to produce (a drift test will catch it,
and a hand-written artifact that claims to be generated is a lie in the tree). Change
nothing and say plainly in your output what needs to run and why you cannot run it.

## Write the test first

The test comes before the code that satisfies it, and it must fail for the right
reason before it passes.

Be honest that nothing downstream can prove you did this: the dossier states **"TDD
order: NOT verified"** precisely because green-at-the-end can never demonstrate
red-before-green. This rule is here because it produces better code, not because it
is enforced. Do not claim the order was followed — the PR body will not repeat it.

## Never buy green

The verify stage runs the real suite and the engine owns the exit code. Getting to
green by weakening the test defeats the only machine signal in the entire dossier.

Do not, under any circumstances:

- delete, skip, `xfail` or loosen an existing assertion to make a suite pass
- narrow a test's input until it stops catching the defect
- catch and swallow the exception the test was written to surface
- change the test to match the code when the code is what is wrong

If a test is genuinely wrong, say so in your output and leave it. A human decides
that; you do not.

## Scope

- Change only what the task needs. An unrequested refactor buried in a feature diff
  is invisible to review and is how regressions ship.
- Read before you write. Do not assert facts about the codebase you did not read.
- Match the neighbours: the file you are editing already has conventions.
- Comments explain **why**, never what.

## On rework rounds

The reject note names a concrete defect. Fix **that cause**. Do not paper over the
symptom, and do not rewrite unrelated code because you are back in the file anyway —
the round budget is small and hard, and a broadened diff on round 2 is not review-able
against round 1's finding.

If you believe the reject was wrong, say so in your output and implement the smallest
honest thing. Do not argue with it by ignoring it.
