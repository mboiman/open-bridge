# Criteria — `review` stage

The standard the reviewer applies. **This is the main knob.** When the reviewer
rejects for the wrong reason, this file is the thing you edit — not the engine, not a
prompt buried in a script.

The record cites `review.md@<sha>` on every review line, so the loop is:

1. Read the trail: `| review | agent | review.md@e5d9a02 | 4m02s | reject → implementing |`
2. `git show e5d9a02 -- skills/board-pilot/criteria/review.md` — the exact standard
   that was applied. Now you can see whether it was too strict, too vague, or simply
   wrong.
3. Edit the rule below.
4. The next round records `review.md@<new sha>`. The change is visible in the trail,
   dated, and attached to an outcome.

You are applying **this** standard, not your general taste. If the standard does not
cover something you dislike, that is not grounds to reject — say it in the annotation
and pass.

## Reject if

- A public function or exported symbol added by this change has no test.
  Do NOT reject for missing tests on private helpers (a leading underscore) — they
  are covered through their public caller.
- An existing test was deleted, skipped, `xfail`ed or loosened. This is the most
  serious finding available to you: it defeats the only machine signal in the dossier.
  Reject it even when the change is otherwise good.
- The change asserts something the code does not support — a comment, a docstring or
  a message that describes behaviour that is not there.
- The diff contains work nobody asked for: an unrelated refactor, a drive-by rename,
  a reformat that buries the real change.
- A guard was removed or weakened: an input check, a fail-closed default, an error
  path turned into a silent pass.
- Comments explain **what** the code does rather than **why**, or a load-bearing
  non-obvious decision has no comment at all.

## Do not reject for

- Style a formatter would fix.
- A design you would have done differently, where the delivered one is defensible.
- Missing tests on private helpers (see above).
- Missing documentation, unless the change makes an existing document false.
- Anything you would phrase as "consider" or "it might be nicer if". Say it in the
  annotation and pass — a reject costs a full rework round from a hard budget.

## Your annotation is the whole message

The implementer sees the annotation and nothing else. Name the **concrete defect and
the file it is in**. "Needs more tests" is unactionable and burns a round to learn
nothing. "`scan.py:41` — `scrub()` has no test for the no-hit path, where it must
return the body byte-identical" is a round well spent.

Rejecting with an empty annotation is refused by the stage itself.
