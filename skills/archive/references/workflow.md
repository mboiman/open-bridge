# Week Archive — Workflow

Trigger: `/archive`, `/archive --force`

## Phase 1: Guard

Check bridge-config.yaml `work.enabled: true`. If not → inform and exit.

## Phase 2: Determine Period

### 2a. Resolve the cadence

Read `bridge-config.yaml` `work.archive_cadence` — one of `weekly`,
`bi-weekly`, `monthly`, `quarterly`, `yearly`. **Missing or unrecognised →
`weekly`**, which is the historical behaviour, so an untouched config
behaves exactly as before.

The cadence decides the period bucket, the destination directory, and the
filename. It is also what `/briefing` reads for its staleness warning, so
the prompt and the archiver can never disagree about what "overdue" means.

| `archive_cadence` | Bucket of a date | Directory | Summary filename |
|---|---|---|---|
| `weekly` | ISO week | `work/archive/weeks/` | `{YYYY}-W{WW}.md` |
| `bi-weekly` | ISO week pair, odd week starts | `work/archive/weeks/` | `{YYYY}-W{WW}+W{WW+1}.md` |
| `monthly` | calendar month | `work/archive/months/` | `{YYYY}-{MM}.md` |
| `quarterly` | calendar quarter | `work/archive/quarters/` | `{YYYY}-Q{Q}.md` |
| `yearly` | calendar year | `work/archive/years/` | `{YYYY}.md` |

`weeks/` keeps its existing meaning and layout, so no instance has to
migrate anything when this key is introduced.

The raw backup sits beside the summary with a `-raw` suffix
(`{stem}-raw.md`), whatever the cadence.

### 2b. Pick the target period

**Content-driven, not calendar-driven.** Look at what's actually in
`work/log.md`, not at today's weekday:

1. Parse the header — `# Week {N}` (or legacy `# KW {N}`).
2. Parse all day-block headers — `^## \S+ ([0-9]{2})\.([0-9]{2})([^0-9.]|$)`
   (any locale weekday token — see rules/language-policy.md). The weekday
   name is display-only; derive the date from the DD.MM capture, never the
   token.
3. Map each day-block date to its **bucket** under the resolved cadence
   (per the table above), and do the same for the header's week.
4. **Archive target = the oldest bucket with content still in the log.**
5. If `target_bucket < TODAY_bucket` → archive it.
6. If `target_bucket == TODAY_bucket` → "no closed {period} to archive; pass
   `--force` to archive the current (in-progress) {period}" — exit unless
   `--force`.

Under `weekly` this is identical to the previous week-only logic; the
bucket function is just `ISO week`.

Why this beats the day-of-week heuristic: heading drift (header says
KW{N} but day-blocks reach KW{N+1}) is the common case after a missed
Sunday archive. The heuristic "Saturday → archive CURRENT week" picks
the wrong target when the user actually wants the old one drained out
first.

`--force` overrides everything and archives whatever the header says.

## Phase 3: Collect

1. Parse log.md day-blocks for the target period
2. `git log --oneline --after="{period_start}" --before="{period_end}"` —
   the bounds come from the resolved bucket, not from a hardcoded Mon/Sun
3. Read `work/archive/days/` for any daily insights
4. Read board.md done section for the month

## Phase 4: Generate Summary

Create the summary at the cadence-resolved path from Phase 2a
(`work/archive/{bucket_dir}/{stem}.md`) using
`work/templates/week-summary.md`:
- Overview metrics (commits, tasks completed, repos touched)
- Daily overview
- Completed tasks
- In-progress tasks
- Highlights and blockers
- Next period priorities

The template's headings say "week" because `weekly` is the default; under a
longer cadence, read them as "the archived period".

## Phase 5: Distill durable memory (propose-then-confirm)

**Runs before the reset, because this is the last moment the rows exist in
`log.md`.** The summary from Phase 4 is a *narrative* of a period; this
phase extracts the handful of facts that should still be true, and still
recalled, long after that period stops being interesting.

Why it exists: `work/archive/` has **no reader**. Nothing in the Bridge
loads an archived summary back into context — not session-start, not
`/briefing`, not `bridge-curator`. Without this phase, archiving is
forgetting with a backup. The memory base *is* read every session (its
`MEMORY.md` index is loaded at start), so distilling into it is what makes
a short cadence safe.

### 5a. Select candidates

Re-read the period's rows and pick only rows carrying **durable** knowledge:

**Distil these**
- A decision **and its rationale** ("chose X over Y because Y needs a schema migration")
- A diagnosis that cost real effort ("launchd can't read `~/Documents` — macOS TCC")
- A convention or constraint discovered the hard way ("`grep -c || echo 0` double-emits")
- A stable fact about the environment (a host, a path, an ID, an ownership boundary)
- A correction of a previously held belief

**Never distil these**
- Commit/PR/push bookkeeping — git and the forge already hold it permanently
- Status transitions, CI results, "started X" / "finished X"
- Anything already superseded by a later row in the same period
- Anything already in the memory base (check `MEMORY.md` first — update the
  existing file rather than adding a near-duplicate)

Expect **few**. A busy period of 60+ rows typically yields 1–3 durable
facts. If the candidate list is longer than ~5, the filter is too loose —
re-apply it rather than proposing a pile the user must wade through.

### 5b. Propose — never write silently

Memory is one of the layers governed by
[`rules/learning-autonomy.md`](../../../rules/learning-autonomy.md): the
Bridge proposes, the human decides. **This phase never writes a memory file
on its own authority.**

Present each candidate compactly:

```
Distilled from Week 27 — 3 candidates

[1] launchd agents cannot read ~/Documents (macOS TCC)
    type: project · why: cost a full session to diagnose; recurs on any
    launchd job pathed into a protected dir
    → memory/project_launchd-tcc-protected-dirs.md
    [a] accept  [e] edit  [r] reject  [d] defer to /bridge-learn

[2] ...
```

Per candidate:

| Choice | Effect |
|---|---|
| `[a]` accept | Write the memory file **and** its one-line `MEMORY.md` index entry now |
| `[e]` edit | User rewrites the text, then accept |
| `[r]` reject | Dropped. Not proposed again for this period |
| `[d]` defer | Written as a proposal instead (below) — decided later via `/bridge-learn` |

Offer `[A] accept all` / `[R] reject all` once the list is shown, for the
common case where the filter did its job.

**Deferred candidates** become normal learning proposals in
`work/_learning/proposals/`, so the existing review surface handles them
with no parallel queue:

```yaml
id: 2026-07-05-archive-w27-launchd-tcc
created: 2026-07-05
source:
  type: archive-distill
  evidence: ["work/archive/weeks/2026-W27-raw.md#L42"]
severity: P3
status: pending
scope: user
target:
  type: memory
  path: memory/project_launchd-tcc-protected-dirs.md
  action: create
proposal_type: structured
```

`scope: user` always — a distilled memory describes *this* instance's
history and never promotes upstream.

### 5c. Memory file format

Follow [`docs/memory.md`](../../../docs/memory.md) exactly — one fact per
file, frontmatter (`name`, `description`, `metadata.type`), `[[wikilinks]]`
between related facts, and **a one-line pointer in `MEMORY.md`, never the
content itself**. The index is loaded into every session, so it must stay
lean; that discipline is the entire reason this phase can be cheap.

`metadata.type` follows the memory model's own vocabulary (`project`,
`reference`, `feedback`, `user`) — a distilled fact is usually `project`
(ongoing work, constraints) or `reference` (an external pointer).

### 5d. Evidence

Every accepted memory records where it came from — the raw archive path
plus, where useful, a line anchor. A memory whose origin can't be traced
is indistinguishable from an invented one.

**Skip this whole phase** when `--force` is combined with an empty period,
or when the memory base is unreachable — warn, continue to the reset, and
say plainly that nothing was distilled. Never block the archive on it.

## Phase 6: Reset log.md

1. Backup to `work/archive/{bucket_dir}/{stem}-raw.md`
2. New log.md with a fresh header for the current period + today's day-block
3. Carry over only unchecked `[ ]` items
4. **Regenerate the `**Active Focus:**` line** — don't keep the stale one
   from the archived period. Build it from the top 3-4 entries in
   `board.md` Doing lane (slug + 1-clause "what's running"), joined
   with ` · `. The previous Active-Focus line often references work
   that just shipped (e.g. a talk that was already given);
   don't carry that forward.

## Phase 7: Upstream Check (conditional)

Skip entirely unless ALL three hold:
1. `bridge-config.yaml` has an `upstreams:` list.
2. At least one entry is either `role: oss-core`, or `role: org-overlay` with a
   `materialize:` block. An org-overlay without that block is authored here, not
   consumed, and is not a subscription.
3. That channel is past its `pull_interval_days` (default 7), measured against the
   last merge from the remote (CORE) or `last_synced` in `overlays.lock.yaml`
   (overlays).

A Bridge with no `upstreams:` list skips this phase; do not load
`references/upstream-summary.md`.

If the prerequisites hold, run the inbound status check from
`references/upstream-summary.md` (the briefing skill ships this reference, archive
borrows it). It is read-only and hands off rather than merging. Nothing is written
back to config: staleness comes from git and the lockfile, never from a
hand-maintained timestamp that can drift from what actually happened.

## Phase 8: Confirmation

Show: "Archived {period_label}: {n} commits, {n} tasks, {n} memories distilled.
Summary: work/archive/{bucket_dir}/{file}."

`{period_label}` follows the cadence — "Week 27", "Weeks 27+28", "July 2026",
"Q3 2026", "2026". Report the distilled count even when zero, so a period
that yielded nothing durable is visibly a decision and not a silent skip.

If upstream had updates: append "Upstream has {n} new commits — merge with `/briefing`."
