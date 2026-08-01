# Criteria — `spec` stage

The standard the planner is held to. Edit this file to change **what a plan must
contain**; the record cites `spec.md@<sha>` on every line the spec stage produced, so
`git show <sha> -- skills/board-pilot/criteria/spec.md` always resolves to the exact
standard that was applied.

This file is read by a model and diffed by a human. Keep it plain prose and plain
rules — no templating, no variables, nothing that needs rendering.

## The task is a story — the analysis is yours

The board item states a **need**: what is wanted, and why it matters. It is written by
a human who deliberately did **not** do the analysis. That work is yours, and it is the
whole of your job:

- **Restating the story is not analysis.** The story is where you start, not what you
  deliver. A plan that reads the need back in different words has done nothing.
- **The analysis is reading THIS repo** — the real files, the real constraints, the
  real shape of the change. A plan grounded in what the story asserts, rather than in
  what the code says, is a guess wearing a plan's clothes.
- **A story may be wrong about the repo, and confidently so.** It may assert a file
  does not exist, that nothing shares a pattern, that some thing is impossible — stated
  as fact, even under a heading claiming it was verified. It was not: the person who
  wrote it did not do the analysis. That is the point.
- **Where the story and the code disagree, the CODE wins.** Follow the code, and say in
  the plan exactly where the story was wrong and what is actually there. Contradicting
  the story with evidence is not insubordination — it is the job being done correctly,
  and it is the most valuable thing a plan can contain.

## A plan must

- Name the **real files** it will touch, with paths that were actually read. A path
  that was guessed is worse than no path: it sends the implementer somewhere that
  does not exist.
- Put the **test first**. State which test is written before which code, and what
  that test asserts. "Add tests" is not a plan.
- State what would make the change **wrong** — the failure mode, not just the goal.
- Name its **uncertainties** explicitly. An unmarked guess is the expensive kind.
- Fit the need as stated. A plan that quietly grows the scope produces a diff nobody
  requested and a review nobody can hold. Departing from the story because the CODE
  says otherwise is not scope growth — that is evidence, and it belongs in the plan,
  named. Adding work the need never called for is.

## A plan must NOT

- Restate the task back as if it were analysis.
- Promise behaviour it cannot check (performance, "improved maintainability").
- Propose changes to CI, branch protection, or the pipeline's own guards. Those are
  human decisions and this pipeline never makes them.
- **Require execution.** The implementer that receives this plan has Read, Glob,
  Grep, Write and Edit — **no shell**. It can produce source text, never a generated
  artifact: no "run the generator and commit its output", no build step, no package
  install, no command of any kind. The first live task on this pipeline died on a
  correct plan whose step 2 was a command the implementer structurally cannot run.
  If the need cannot be met by editing source files alone, say exactly that (see
  "Reject the task" below) and name the command a human would have to run — do not
  plan around the constraint by telling the implementer to hand-write what a
  generator should produce.

## Reject the task, don't invent one

If the need cannot be met as described — it is ambiguous, it contradicts the code, or
it asks for something the repo does not have — say that plainly and explain why. A plan
that says **"this cannot be done as asked, because X"** is a valid and useful outcome.
Inventing a plausible adjacent task to have something to build is not.

This is not the same as the story being wrong about a detail. A story that misdescribes
the repo but states a real need is **workable**: correct the story, cite the code, plan
the work. Reject when the *need itself* cannot stand — not merely because the story got
a fact wrong.
