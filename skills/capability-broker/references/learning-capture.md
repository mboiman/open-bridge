# Learning Capture — close the loop, make it compound

Every outcome (provision / scaffold / one-off / decline) writes a learning, so
the same gap never recurs unaddressed. Two artifacts: a **proposal** (reviewable
in `/bridge-learn`) and a **ledger row** (recurrence tracking).

## 1. Proposal

Write `work/_learning/proposals/<YYYY-MM-DD>-capability-<slug>.md` against
`work/_learning/_schema.proposal.yaml` (it requires `capability-gap` in the
`source.type` enum — see design.md § 10).

```markdown
---
id: 2026-06-25-capability-heic-convert
created: 2026-06-25
source:
  type: capability-gap
  evidence:
    - "work/_learning/capability-gaps.md#2026-06-25-heic-convert"
    - "session: <session-id>.jsonl"          # the request that surfaced it
severity: P2
status: pending
scope: core                                   # generic → open-bridge roadmap candidate
target:
  type: skill                                 # or: tool dep / mcp / account
  path: skills/image-convert/SKILL.md
  action: create
proposal_type: structured
estimated_effort: "1h"
---

## What

Gap surfaced 2026-06-25: convert HEIC → JPG. No skill, no wired tool.
Route taken: BOTH — provisioned `imagemagick` (CLI), scaffolded `image-convert`.

## Why this scope

Image conversion is generic → `scope: core` → open-bridge roadmap candidate
after `/bridge-learn` accept and `/bridge-sync`.

## Artifacts

- tool: `imagemagick` (brew) — recorded in skill `allowed-tools` + checkup entry
- skill: `skills/image-convert/` (drafted by skill-creator)

## Acceptance

- [ ] `magick` on PATH, checkup entry green
- [ ] skill triggers on "convert image", scope-validator passes
```

Notes:
- **scope** routes the *artifact*: `core` = generic (→ open-bridge roadmap via
  `/bridge-sync` after accept); `user`/`org` = instance-specific (local / an
  org-internal overlay). Choose by "would any Bridge user want this?".
- **evidence** is mandatory and concrete (no invented proposals) — point at the
  ledger row + the originating session.
- `status: pending` only. capability-broker never accepts its own proposals;
  `/bridge-learn` does.
- For a **decline**, still write a short proposal with `status: rejected` is
  wrong — instead just write the ledger row (below). A proposal is for an
  artifact to land, not for a non-decision.

## 2. Ledger — work/_learning/capability-gaps.md

Append-only, one row per detected gap (see the file's own header for format).
Carries the **recurrence counter** that turns a repeated one-off into a persist
proposal:

```markdown
| 2026-06-25 | heic-convert | "convert HEIC photos to JPG" | scaffolded | 1 | skill image-convert + imagemagick |
| 2026-06-25 | linear-pull  | "show my open Linear issues" | one-off    | 1 | — |
| 2026-06-25 | foo-export   | "..."                         | declined   | — | cooldown |
```

- **disposition** ∈ `provisioned | scaffolded | both | one-off | declined`.
- **recurrence**: increment when the same gap-slug reappears. At
  `recurrence_escalate_after` (config, default 2) for a `one-off`, emit a
  `capability-gap` proposal recommending persistence — the one-off has earned a
  skill.
- **declined / silenced** rows feed the cooldown check in `gap-detection.md`
  so the gap isn't re-offered within `decline_cooldown_sessions`.

## 3. Memory (optional)

A non-obvious decision behind a tool choice (why A over B, an auth gotcha worth
keeping) rides the existing `proposal target.type: memory` path into MEMORY.md —
human-approved via `/bridge-learn`, never auto-written (per
`rules/learning-autonomy.md` Layer C).

## 4. Roadmap candidate

A `scope: core` proposal, once accepted in `/bridge-learn`, is a contribution
candidate: `/bridge-sync` carries it to open-bridge (OSS) — the new generic
capability becomes part of the shared Bridge, closing the compounding loop.

## What capture does NOT do

- ❌ Touch `work/log.md` / `work/board.md` directly here — task/log mechanics
  follow the normal standing-orders if the work warranted a task.
- ❌ Accept, apply, or promote anything. Capture writes `pending` proposals and
  ledger rows; `/bridge-learn` + `/bridge-sync` own accept and promotion.
