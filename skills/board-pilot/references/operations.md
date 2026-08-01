---
scope: core
summary: Operations & troubleshooting for a live board-pilot deployment — the silent never-arms trap, recovering a parked item, the GitHub GraphQL rate-limit disguise, and the lock-liveness restart caveat.
type: reference
last_updated: 2026-07-15
related:
  - ../SKILL.md
  - ./transparency.md
---

# board-pilot — operations & troubleshooting

Operational gotchas learned running the loop against a live GitHub Project. All
of them are generic to **any** board-pilot deployment, not tied to one project's
wiring — keep them here (the skill is the structural home) rather than in a
private memory store.

## 0. "Nothing is happening" — the `status_field` trap

**Symptom:** the poller ticks cleanly, logs no error, and **never arms
anything**. Cards sit in the trigger column forever.

**Cause, most of the time:** `cli.py` defaults `board.status_field` to
**`"Status"`**. GitHub Projects ships a *built-in* `Status` field, so on a board
whose real lifecycle field is named something else (`Workflow`, `Stage`, `State`
…), the engine reads the built-in `Status`, never sees the trigger value, and
does nothing — **silently**. There is no error to grep for: reading the wrong
field is indistinguishable from an empty column.

**Fix:** set `board.status_field` to the field's real name.

**Confirm which field is which** before guessing — the live board is the truth,
never the registry snapshot:

```bash
gh project field-list <number> --owner <owner>
```

If a *built-in* `Status` exists **and** is populated, this trap is live for you.
Same class of failure, same check: a trigger value that differs from the board's
option by a character or a case (`Ready For Development` vs
`Ready for Development`) never arms either.

## 1. Recovering a parked item — clear `Pipeline`, then re-drag

A parked item does **not** resume on its own, and nothing in the engine unparks
it. The recovery gesture works **today, with zero engine change**, and it is the
one every operator needs:

1. **Clear the `Pipeline` field** on the card (set it to empty).
2. **Set the lifecycle field back to the trigger value** (re-drag the card).

The next tick re-arms it. Why this works: arming is gated on
**`status == trigger_status` AND `pipeline` is empty**. `Pipeline` is the durable
arm latch — nothing but a human ever clears it, which is exactly why an item can
never re-arm itself and why a wiped snapshot cannot mass re-arm the board.

**Both halves are required.** Clearing `Pipeline` alone does nothing if the card
is sitting in `Blocked` (the park column) — the status conjunct fails. Re-dragging
alone does nothing while `Pipeline` still holds a value — the latch conjunct fails.

**What re-arming resets:** the durable bounce counter goes back to `0`, so the
item gets a **fresh rework budget**. That is intended (a re-armed item must not
inherit a stale count and re-park instantly) — but it does mean the gesture hands
an item another full `max_rounds` of paid runs. Fix the underlying cause first;
this is a rearm, not a retry.

> **Never clear `Pipeline` on an item that is mid-flight.** The arm loop runs
> outside the per-item lock, so clearing the latch under a running stage would
> re-arm the item and zero its counter beneath the worker. The engine defends
> itself here — the arm loop skips any item whose lock is held by a live worker —
> but the honest advice is to park or let the stage finish first.

## 2. `gh project … unknown owner type` is the GraphQL rate limit in disguise

The tick reads the board with `gh project item-list`, which goes through
GitHub's **GraphQL** API. That API has a budget of **5000 points/hour, per
account** (per GitHub user-id) — **shared** across every machine and token
authenticated as that account.

When the budget is exhausted, `gh project item-list` fails with

```
gh failed (1): gh project item-list … unknown owner type
```

This is **not** a real owner / SSO / permission error — it is the rate limit in
disguise (the owner-resolution sub-query is what runs out of budget first). The
loop surfaces it as `board fetch failed: … unknown owner type` and armed items
**stall at `queued`** until the budget recovers.

**Confirm cheaply.** `gh api rate_limit --jq .resources.graphql` is **free** (it
does not consume budget) and shows `remaining` + the `reset` epoch. The window
is **fixed-hourly**, not rolling — it recovers all at once at the top of the
hour, not gradually.

**The loop itself is cheap; watchers are the drain.** At a ~60 s tick the poll
costs ~60 GraphQL calls/hour ≈ 1–3 % of budget. The bucket gets exhausted by a
**one-off manual setup burst** (creating fields/options, bulk item edits) or by
an **aggressive monitor** — a watcher polling `gh project item-list` every ~40 s
**alongside** the loop competes for the same bucket, drains it, and **blinds the
loop**. This happened twice in one session.

**Monitor a running loop cheaply — never with a competing `gh project` watcher:**

- **Box service log over SSH** — `tail ~/Library/Logs/<unit>.out.log` — is free
  (no API). This is the primary way to watch progress.
- **REST, not GraphQL, for status.** `gh pr list` / `gh issue view` ride a
  separate, healthier REST budget; prefer them over `gh project item-list` when
  you just want to know "did the PR open yet?".
- Spend at most **one** `gh project` call to confirm the final board state.

## 3. "bounced Nx but no reject note reached it" — a real park, not a glitch

**Symptom:** an item parks with `bounced 1x but no reject note reached it (lost
comment or denied read-back) — refusing a blind rework`.

**What it means:** the durable counter says this item was rejected and sent back
for rework, but **the note explaining why never arrived**. The reject comment
failed to post, or the read-back refused it (see
[`transparency.md`](transparency.md) — the read-back is fail-closed and
round-scoped: it demands the note's round match the counter *exactly*).

**Why the engine parks instead of re-running:** re-dispatching here would hand the
code writer a rework instruction with **no feedback in it**. It would burn the
entire rework budget producing nothing — while the ledger reported a healthy
loop the whole time. The park converts a silent, expensive failure into a loud,
free one. **Zero LLM spend**, human-recoverable.

**As an operator:** this is a signal that the **comment channel** is broken, not
the code. Check, in order:

1. Can the engine's identity post at all? A missing comment-write permission
   presents exactly like this.
2. Does a reject note for that round actually exist on the issue?
3. Does the note's round match the item's counter? A note from round 1 does not
   satisfy a counter at 2 — and that mismatch is a *deliberate* refusal, not an
   off-by-one to "fix".

Then recover with the gesture in § 1. Do **not** work around this by relaxing the
read-back: fail-closed here is what makes "the agent got the feedback" a fact
rather than a hope.

## 4. Lock liveness: don't `kickstart -k` while a stage is running

Each in-flight item holds a per-item lock. The engine reclaims a lock only when
the holder is **both** heartbeat-stale **and** pid-dead — that double condition
is the safety that prevents two ticks from dispatching the same item (a
20-minute job under a 1-minute poll).

`launchctl kickstart -k` mid-stage kills the worker, but the lock then sits
**pid-dead-but-not-yet-stale**: re-dispatch is blocked until the stale window
(~15 min) elapses. There is **no** double-dispatch (the safety working as
designed), but you pay a ~15-minute delay before the item moves again.

**So:** to nudge a stuck loop, prefer a clean restart **between** ticks, not a
`kickstart -k` in the middle of a running stage. If you must kill mid-stage,
expect the stale-window delay before the item re-dispatches — it is not stuck,
it is waiting out the liveness guard.
