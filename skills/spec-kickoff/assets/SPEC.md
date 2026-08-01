# yaml-language-server: $schema=
---
# SPEC — produced by the spec-kickoff skill.
# A good spec is SELF-CONTAINED: it names the files/interfaces involved, states
# what is out of scope, and ENDS IN an end-to-end verification step that proves
# the build works. (Anthropic Claude Code best practices.)
spec_version: 1
slug: <slug>
created: YYYY-MM-DD
autonomy: tight        # tight | medium | wide — how much leash the build gets
status: draft          # draft | approved | building | verified
---

# SPEC — <Title>

## 1. Goal — the human owns this

> The *decision* or *outcome* this drives — NOT the task. One short paragraph.
> "Done" means: a human can make <decision> / the user can <do X> / <metric> holds.

<…>

## 2. Non-goals / out of scope

- <explicitly excluded — name it so the agent doesn't drift into it>

## 3. Context & touchpoints

- **Files / interfaces involved:** `<path>`, `<api/module>`, …
- **Constraints:** perf / security / data / deadline / cost — <…>
- **Context the agent must read first:** <CLAUDE.md · rules · a curated knowledge
  folder (raw→wiki→outputs) · prior art>

## 4. Requirements — EARS

> One requirement per line, in EARS. Each becomes an acceptance check in §6.
> The "IF … THEN" (unwanted-behaviour) lines are where edge cases & failure modes
> get forced into the open — don't skip them.

- **Ubiquitous:** THE SYSTEM SHALL <response>
- **Event:** WHEN <trigger> THE SYSTEM SHALL <response>
- **State:** WHILE <state> THE SYSTEM SHALL <response>
- **Optional:** WHERE <feature is included> THE SYSTEM SHALL <response>
- **Unwanted (edge/failure):** IF <condition> THEN THE SYSTEM SHALL <response>

## 5. Slices & checkpoints — agile, not waterfall

- [ ] **Slice 1** — the *thinnest end-to-end slice that has a verifiable check*. ← build this first
- [ ] Slice 2 — <deferred>
- [ ] …

> Each slice: build → verify (§6) → checkpoint (commit / rewind point) → next.
> Don't dump the whole thing in one pass.

## 6. Verification plan — the spec ENDS here

> Every acceptance criterion gets a check. Preference: **deterministic > external
> signal > judge.** Anything that can't be checked → mark **UNVERIFIABLE** and
> decide: make it verifiable, or don't ship it.

| # | Acceptance criterion (from §4) | Check type | Concrete check | Gate strength |
|---|--------------------------------|-----------|----------------|---------------|
| 1 | <criterion, precise pass/fail> | deterministic / external-signal / judge | `<test · build · lint · CI · deploy probe · fixture diff · reviewer>` | one-prompt / goal-cond / stop-hook / 2nd-opinion |
| 2 | … | … | … | … |

- **Adversarial review before "done":** <native fresh-subagent `/code-review` · or
  `/codex:adversarial-review`> — show evidence, not assertions; flag **only**
  correctness / requirement gaps.

## 7. Key decisions to confirm — verify explicitly

- [ ] <assumption or design decision the agent made> — confirmed by human?
- [ ] …

## 8. Open questions

- <unresolved — resolve or accept as risk before building the affected slice>

## 9. Environment checklist — Layer 3 (point at what exists)

- [ ] **Context loaded** — CLAUDE.md / rules / knowledge folder the agent stands on
- [ ] **Reusable?** — if you'll do this repeatedly, make it a skill (skill-creator)
- [ ] **Guardrails** — never-do paths protected (PreToolUse exit-2 hook?) · ask-first
      (auto-mode) · always-do (/permissions allowlist)

## 10. Handoff

Build slice 1 against this spec in a fresh session → verify with its check →
checkpoint → repeat. **You own the understanding; the agent does the thinking.**
