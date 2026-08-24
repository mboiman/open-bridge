# Daily Briefing — Workflow

Trigger: `/briefing`, `/briefing --quick`, `/briefing --html`

| Argument | Effect | Default |
|----------|--------|---------|
| `--quick` | Focus box + overview only, no GitHub sync | false |
| `--html` | Additionally render HTML dashboard | false |

**RULE: Never block. Always let the user continue working.**

## Architecture: Parallel Data Collection

Briefing collects data in **4 parallel streams** (not sequential!):

```
Stream A (local):          Stream B (trackers):        Stream C (optional):        Stream D (channels):
├─ log.md last entries     ├─ fan-out over             ├─ Calendar (if available)  ├─ Channel collectors
├─ board.md stats          │  trackers/*.md            ├─ imports scan             │  (signal, RSS, etc.)
├─ tasks/*/STATUS.md      │  (github, ado, linear…)   ├─ Health checks            └─ Action detection
├─ git log (7d, all repos) └─ merge + normalize        └─ Meeting transcripts
└─ Week + day-block check
```

**Stream A** always runs. **Stream B** skipped with `--quick`. **Stream C+D** are best-effort.

Stream B is **provider-pluggable**: each `trackers/{name}.md` file is a
playbook Claude reads to fetch items from one external system (GitHub
Projects, Azure Boards, etc.). See `trackers/README.md` for the contract.

## Phase 0: Smart Detection + Day Block

```bash
# Archive cadence — drives the staleness threshold below. Never hardcode it:
# a `scope: core` skill reads instance thresholds from config (AGENTS.md
# § Generic CORE Skills). Missing/unrecognised → weekly (historical default).
CADENCE=$(yq -r '.work.archive_cadence // "weekly"' bridge-config.yaml 2>/dev/null)
case "$CADENCE" in weekly|bi-weekly|monthly|quarterly|yearly) ;; *) CADENCE=weekly ;; esac

# Header KW — try German "KW <N>" first, fall back to English "Week <N>".
LOG_KW=$(head -1 work/log.md | grep -oE '(KW|Week) [0-9]+' | grep -oE '[0-9]+')
TODAY_KW=$(date +%V)
TODAY_WEEKDAY=$(date +%u)  # ISO weekday 1=Mon..7=Sun, locale-independent (weekend = >=6)

# Day-block KW drift — parse newest day-block and compare. Independent of header.
LATEST_BLOCK=$(grep -oE '^## \S+ [0-9]{2}\.[0-9]{2}([^0-9.]|$)' work/log.md | tail -1)
# If LATEST_BLOCK exists: convert "DD.MM" + current year → ISO date → KW via `date -j -f "%Y-%m-%d" ... +%V`
```

The day-block weekday token is produced by `date '+%a'` (locale-driven:
Mon/Mo/lun. …) — never hand-mapped; parsers match any token via
`^## \S+ DD.MM`.

### Distinct drift sources

Two **independent** signals about CORE/upstream code state — don't conflate:

| Signal | What it means | Where checked |
|--------|--------------|---------------|
| **Origin-CORE drift** | Local user branch is behind `origin/development` (the CORE source for this fork) — usually because someone else merged a CORE PR | Phase 0 row "git log HEAD..origin/development" below |
| **Inbound drift** | An upstream declared in `bridge-config.yaml` `upstreams[]` has moved ahead: the `role: oss-core` entry (CORE, e.g. `bks-lab/open-bridge`) or any `role: org-overlay` entry carrying a `materialize:` block (an org overlay). | Phase 6, which runs whenever an `upstreams[]` list exists. The git remote is resolved by matching its URL against `upstreams[].repo`, never by a remote NAMED `upstream`: that name is arbitrary and usually absent. |

An instance with no `upstreams:` list (the OSS seed repo itself, or a fresh clone) has only Origin-CORE drift in scope; Phase 6 is a no-op there.

### Detection table

| Condition | Action |
|-----------|--------|
| Log spans **more than one `{CADENCE}` bucket** (oldest day-block's bucket < today's bucket) | **Archive overdue.** Loud warning: "log.md spans {N} {CADENCE} periods; oldest is {LABEL}. Archive it now?" → if y, run `/archive` inline (which handles oldest-period semantics, see archive workflow) |
| Header label disagrees with the day-blocks, but all blocks sit in **one** bucket | Header drift only, no archive due: silently correct the header to the current period's label and continue. Do not prompt — there is nothing to archive. |
| Day block missing for today | Append new day block (see format below) |
| Weekend (Sat/Sun) | Warning: "Weekend. Check in anyway?" — proceed if user confirms |
| Log spans 2+ buckets of the configured cadence | Covered by the overdue row above — do **not** raise a second, independent warning. Under `yearly` a 40-week log is not overdue, and a duplicate week-based check would contradict the user's own setting. |
| `git log HEAD..origin/development --oneline` has results | **Origin-CORE drift.** Show count + first commit subject. Ask if `git merge development` desired (CORE/USER paths shouldn't conflict; CLAUDE.md edits sometimes do). |
| `upstreams:` configured AND any channel past its `pull_interval_days` | Run the inbound status check, see `references/upstream-summary.md`. It covers CORE and overlays and is read-only; staleness is derived from git and `overlays.lock.yaml`, so there is no timestamp to write back. |
| All current | Proceed to data collection |

**New day block format (append to end of log.md):**

```markdown
---

## {Weekday} {DD.MM}

**Focus:** {1-line daily focus}

### Goals
1. {auto-populated, see below}
2. ...
3. ...

### TODO (rolling)
- [ ] ...

<details open><summary>Worklog (0)</summary>

| Timestamp        | Type | Context | What |
|------------------|------|---------|------|

</details>
```

Use short header `## Mon 14.04` (not `## Monday 14.04.2026`, not `## 2026-04-14`) —
the regex above parses any weekday token via `^## \S+ DD.MM`. Worklog rows carry
a full `YYYY-MM-DD HH:MM` timestamp (`date '+%Y-%m-%d %H:%M'`); bump the `(N)`
count in the `<summary>` on each append.
Canonical skeleton: `work/templates/day.md`.

**Goals auto-populate** (3-bullet max, in priority order):
1. Pull `### TODO (rolling)` items from the previous day-block — these
   are explicit carry-overs the human marked open.
2. If <3 items collected, fill from `board.md` Doing lane **in two passes**
   (Pass A surfaces customer momentum, Pass B surfaces active problems):
   - **Pass A — Customer momentum:** Doing-tickets where `context.mandant`
     resolves to an external customer (i.e., not `bridge`, `intern`,
     `personal`, `acme`) AND `last_updated >= 3 days ago`,
     **regardless of status marker**. A 🟢 customer task that's been
     idle for a week is exactly where the user needs to push, not where
     to ignore.
   - **Pass B — Active problems:** top tickets with status `🟡` or `🔴`
     (the existing rule, now subordinate to customer momentum).
3. If still <3, leave the slot empty rather than padding — empty slots
   are honest signal.

**Why two passes:** for personal infrastructure tasks, 🟢 = "leave it
alone." For customer engagements, 🟢 + idle days = "your relationship
is going stale, surface it." The state marker is an internal-health
signal, not an external-action signal.

The human can edit/replace freely; auto-populate is a starting point,
not a constraint.

### Customer-vs-Infra Framing (for free-form answers)

When the user asks a free-form question outside the mechanical briefing
output — *"what's open / where do I need to act"* — bucket findings by
**surface risk**, not by state marker color:

1. **Customer engagements** — extern, Revenue-/Relationship-Surface
2. **Applications / acquisition** — external, pipeline surface (only if you track an application pipeline)
3. **External blockers** (external blocker reactions, kickoffs, recruiter replies, …)
4. **Personal infrastructure** (CF-Setup, Drift-Sweeps, Backups)
   - Backups: if `infra/backups/topology.yaml` exists, run the health check the
     backup executor ships (here: `infra/backups/check-health.sh`) rather than
     reading `_state.yaml` by hand. Surface a ⚠ block **only** for a dead job, a
     wedged process, or a pipeline with no successful run inside its schedule
     window; all-green stays silent. Validation + staleness rules:
     `infra/backups/README.md`.

     > **Never derive backup health from `exit_code`.** rclone returns `1` when a
     > *single* file fails and `10` on `--max-duration`, which is the normal
     > outcome for a running mirror — both come and go without anything being
     > wrong. Reporting them as failures is alarming on noise, and it once led to
     > a "replace the drive" recommendation while SMART, mount and free space
     > were all fine. The three signals that carry meaning, in order: the service
     > manager says the job is loaded · no sync process is wedged (long-running
     > with a log that stopped growing) · a *successful* run happened inside the
     > schedule window (`last_ok`, never `last_run` — a run that fails instantly
     > still stamps `last_run` and would paint a total outage green).
5. **Admin debt** (cleanup, stale tasks, issue hygiene)

A customer task with `🟢` + `last_updated ≥ 3d` belongs in bucket 1,
**not** in "already running, no action needed". Customer engagement is
push-model — if nobody surfaces it, the relationship goes stale.

**IMPORTANT: Never block.** If KW detection fails or log.md has unexpected format, warn and proceed. The briefing must always complete.

## Phase 1: Parallel Data Collection

**Start all streams simultaneously.** Collect results, then render output.

### Stream A: Local State

1. **log.md** → Last entries, today's goals
2. **board.md** → Active/queue/done counts
3. **tasks/*/STATUS.md** → Per task: status (backlog/doing/review/done; blocked = flag), last update
   - **Skip** dirs whose name starts with `_` (e.g. `_meetings/`) — those are
     containers, not tasks.
4. **tasks/-board reconciliation** (drift-sweep — surface as warning):

```bash
# zombies: dir in tasks/ but no row on board.md
for dir in work/tasks/*/; do
  slug=$(basename "$dir")
  [[ "$slug" == _* ]] && continue
  grep -q "$slug" work/board.md || echo "zombie: $slug"
done

# orphans: board row references work/tasks/<slug>/ but dir missing
grep -oE 'work/tasks/[a-z0-9-]+/' work/board.md | sort -u | while read -r p; do
  [ -d "$p" ] || echo "orphan: $p"
done
```

If `zombie` or `orphan` count > 0 → emit Phase 4 warning
"task drift: N zombies, M orphans → Phase 2 sweep wird vorgeschlagen."

5. **tasks/_meetings/*/triage.md → offene Debrief-Punkte** (the debrief→briefing
   handoff). A debrief **captures** without forcing decisions; the **briefing** is
   where the open points get decided. So this is the ONE place `_meetings/` IS read
   (the task-scan in #3 still skips it as a container). The meeting home + triage
   filename come from `work.meetings.home_dir` + `triage_file` in bridge-config
   (the central meeting-layout SoT). For each
   `{work.meetings.home_dir}/*/{triage_file}` (i.e. `work/tasks/_meetings/*/triage.md`)
   whose frontmatter is `status: pending-triage`,
   surface its unchecked (`☐`) action-item rows as an **„Offene Debrief-Punkte"**
   block:
   ```
   Offene Debrief-Punkte:
     • <meeting> — N offene Punkte → entscheiden: work/tasks/_meetings/<slug>/triage.md
   ```
   Count unchecked rows in the `| … | ☐ |` table; show the meeting + count + path
   (not every row — the user opens triage.md to decide). Drop a meeting from the
   surface once `status: triaged` or all rows are checked (`☑`). This is what lets
   the user "decide the open points later at the briefing" instead of mid-debrief.

5b. **tasks/_meetings/*/summary.md → offene Pflichten** (the second debrief→briefing
   handoff, driven by `work.meetings.tracked_obligations`). A debrief captures; if
   there was no time to extract tasks or write the wiki protocol, the briefing
   **reminds** — so the obligation is never silently lost. For each
   `{work.meetings.home_dir}/*/{summary_file}` read its frontmatter and surface:
   - **`tasks.status: pending`** AND no `triage.md` yet → *"Tasks noch nicht gezogen"*
     (the meeting was captured but its action items were never extracted/triaged).
   - **`wiki.status: pending`** (set whenever the meeting's `context.scope` appears
     in `tracked_obligations.wiki_protocol.required_for_scopes`) → *"Wiki-Protokoll
     fehlt"*. Those meetings owe a wiki protocol, with a source-pointer back to the
     filed original. Group and count them; writing is gated (`/debrief` Phase 6, on
     demand), the briefing only **surfaces the backlog**:
   ```
   Offene Meeting-Pflichten:
     • Wiki-Protokoll fehlt (N): <slug>, <slug>, … → /debrief <slug> Phase 6 (gated push)
     • Tasks offen (M):         <slug>, …          → /debrief <slug> Phase 5
   ```
   Drop a meeting once `wiki.status: done`/`n/a` resp. `tasks.status: done`. Don't
   re-render every meeting — cluster by obligation type (Phase 3.5 cluster rules apply
   when N≥threshold). This is how a scope that owes documentation reliably reaches the
   wiki even when the debrief was capture-only.

5. **Git Activity (7 days)** for all repos in ecosystem.yaml:

```bash
# Per repo: commit count per day for sparkline.
# Same-day --since/--until pattern avoids the BSD-date "negative offset" bug
# that bit on i=0 (date -v--1d errors out).
for repo in $(repos_from_ecosystem); do
  if [ -d "$repo/.git" ]; then
    for i in 6 5 4 3 2 1 0; do
      day="$(date -v-${i}d +%Y-%m-%d)"
      git -C "$repo" log --oneline \
        --since="${day} 00:00" --until="${day} 23:59" 2>/dev/null | wc -l
    done
  fi
done
```

**Sparkline:** Commit counts → Unicode blocks `▁▂▃▄▅▆▇█`
- 0 commits = `▁`, maximum = `█`, linearly scaled

### Stream B: Trackers — provider fan-out (skip with --quick)

**Do not hardcode any tracker-specific commands here.** Stream B
discovers enabled providers at runtime and delegates to their
playbook files.

#### Discovery

```
for file in trackers/*.md (except README.md, _*.md):
  name = basename(file, ".md")
  enabled = bridge-config.yaml → integrations.{name}.enabled
  if enabled is true:
    candidates += file
```

No hardcoded list of providers. A user who drops a new `linear.md`
with a matching `integrations.linear` config block gets it picked up
automatically.

#### Execution

For each candidate, **in parallel**:

1. Read `trackers/{name}.md` (the playbook)
2. Read `bridge-config.yaml` section `integrations.{name}`
3. Follow the playbook's **Collect** section: run the CLI commands it
   describes, with the config as parameters
4. Normalize each item into the shared schema from `trackers/README.md`
5. Return the normalized list

Per-command timeout: 10 seconds. If a command times out or fails,
emit a warning to the user and continue with whatever the provider
has already produced. **Never abort the briefing for a tracker failure.**

#### Merge

After all providers return:

1. Concatenate all item arrays
2. Deduplicate by `url` (if the same item is returned by two sources,
   keep the one with the richer field set)
3. Sort by `(category ASC, changed_at DESC)` — so `open` items come
   first, then `qa`, then `done`, and within each bucket the most
   recently changed on top
4. Group by `category` for rendering (see Phase 4 below)

#### Per-provider failure modes

| Condition | Action |
|---|---|
| Required CLI not installed | Warning, skip that provider |
| CLI not authenticated | Warning with auth hint, skip that provider |
| Config malformed | Warning, skip that provider |
| All commands >10s | Warning, skip that provider |
| Zero items from every provider | Omit Stream B section entirely |

#### Open PRs (GitHub, org-wide)

Boards track *issues*; this surfaces *open pull requests* across every org —
a different dimension. Runs only when `integrations.github.enabled: true`;
skipped under `--quick` / `--skip-trackers` like the rest of Stream B.

1. **Resolve orgs** — distinct `org` values from
   `ecosystem.yaml.github_projects` (config-driven, e.g. `your-org`,
   a partner org). Do not hardcode.
2. **Collect per org** (10 s timeout, never block):
   `gh search prs --owner {org} --state open --limit 200 --json repository,number,title,author,createdAt,isDraft`
3. **Filter archived — LIVE, not the search index.** The `archived:false`
   search qualifier runs off GitHub's search index, which lags freshly-
   archived repos (a just-archived repo's PRs keep showing as open for a
   while). So fetch the live archived set per org —
   `gh repo list {org} --archived --limit 300 --json name --jq '.[].name'`
   — and drop any PR whose `repository.name` is in it. Open PRs on an
   archived (read-only) repo are dead weight: they can never merge, only
   clutter the count. **Trust the per-repo `isArchived` flag, never the
   qualifier.**
4. **Split human vs bot** — author login matching `/dependabot/i` (or any
   `[bot]` suffix) → bot bucket; everything else → human bucket. Mark PRs
   whose author == `integrations.github.assignee_me` as own (rendered bold).

Render via the `── Open PRs ──` section in Phase 4 (human PRs listed,
bot PRs collapsed to one count line). Omit the section entirely when zero
open PRs remain after the archived filter.

### Stream C: Companion Data

1. **Calendar** — capability-based discovery. Iterate
   `bridge-config.yaml` → `integrations.context_sources.*` and pick
   every provider where `enabled: true` AND `provides` contains
   `calendar`. The `skill:` field names the installed pull-skill
   (e.g. `email-manager`, `google-calendar`, `ical-reader`). Same
   pattern as Stream B trackers and `/debrief` Phase 1.5 — no
   hardcoded provider name.
   - Pull events for today, merge across providers, dedupe by event id
   - Show total meeting hours vs. focus time
   - Skip silently if no matching provider

   **1a. Calendar Cross-Reference (past-due STATUS guard)**

   For every Stream-A finding `STATUS-Datum X.Y.YYYY past-due` (i.e.,
   `STATUS.md` lists a Pflicht-date < today and status hasn't advanced
   past that phase), run a calendar lookup before flagging the date
   as "missed/vorbei":

   1. Build search tokens from `work/tasks/<slug>/STATUS.md`:
      - slug tokens (split on `-`)
      - headline words (skip stopwords)
      - explicit `sync.calendar_keywords[]` if set (preferred — see
        `work/templates/_schema.status.yaml`)
   2. Query a local calendar tool (default `icalBuddy` if installed:
      `command -v icalBuddy`, or `BRIDGE_ICALBUDDY` env var). Window:
      `[past-due-date, past-due-date + 30 days]`.
      ```bash
      icalBuddy -nc -n -df "%Y-%m-%d" -tf "%H:%M" -b "" \
        -po "title,datetime" \
        -eep "notes,attendees,location,url,priority,attachments" \
        eventsFrom:<past-due-date> to:<past-due-date+30d> \
        | grep -iE "<token1>|<token2>|..."
      ```
   3. If ≥1 match: render the task as
      **`🟢 moved to <new-date> (calendar confirmed)`**, not
      as `❓ vorbei!`.
   4. If 0 matches in the 30-day window: render as
      **`⚠ wirklich verpasst — Kalender-Suche leer`** (now the warning
      is honest).
   5. If `icalBuddy` not installed: silent skip — leave the original
      `vorbei!` rendering. Don't error.

   **Why:** STATUS.md is human-edited text and drifts. The calendar is
   the source of truth for reschedulings. Bridge must consult it before
   shaming a missed date. This rule fired on 2026-05-18 (schick-cybersec
   Kickoff was moved from 16.05 → 26.05; STATUS.md showed 16.05 stale,
   calendar held the correct entry).

2. **imports scan** (all file types):
   - Resolve imports directory: `bridge-config.yaml` → `work.imports_dir`
     (fallback: `work/imports/`)
   ```bash
   find "${imports_dir}" -type f -not -name '.DS_Store' -not -name '.gitkeep' 2>/dev/null
   ```
   Group by file type and show counts.

3. **Meeting transcripts**: If `.md`, `.txt`, `.vtt`, `.srt` found in the imports dir:
   - Ask: "{N} transcripts ready. Process? [y/n]"
   - Yes: run `/debrief` workflow — includes task-reconciliation against
     configured projects (`skills/debrief/references/task-reconciliation.md`)
     and distribution email to meeting participants
     (`skills/debrief/references/distribution-email.md`) when the meeting-type
     config enables them. briefing does **not** duplicate this logic.
   - No: continue, transcripts stay in the imports dir

4. **Health checks** (if defined in ecosystem.yaml, 3s timeout)

5. **Application pipeline scan** (optional — only if you track an application
   pipeline and `applications.enabled: true`):
   - Read your applications standing order (e.g.
     `protocols/standing-orders/user/applications.md`) for the surface rules
   - Walk `work/streams/applications/YYYY-MM/*/STATUS.md` (current + previous month) and
     compute days-since-last-action per status.
   - Surface compactly when ANY threshold trips:
     - `draft` >2 days unsent
     - `sent` >10 days no reply
     - `reply` >3 days awaiting next step
     - `interview` upcoming within 48h
   - If nothing trips, omit the section entirely (don't render an empty block).
   - This is a **digest**, not the full pipeline.

### Stream D: Channel Activity

Two discovery sources, both opt-in. Capability-based, no hardcoded
provider names:

1. **Channels with `checkin.enabled: true`** in `infra/channels/*.yaml` —
   the legacy hook for outbound-channel collectors that also publish
   inbound activity (e.g. an iMessage relay that surfaces incoming
   alerts).
2. **`integrations.context_sources.*`** in `bridge-config.yaml` where
   `enabled: true` AND `provides` contains `chat` or `calls` — the
   generic pull-source pattern (Teams, Slack, Signal, ...). Same
   capability-based discovery as Stream C calendar and `/debrief`
   Phase 1.5.

For each matching source:
- Run the configured skill / collection script
- Extract action items, messages, notifications from today
- Action items with high confidence become focus suggestions

Skip the entire stream silently if no source matches.

## Phase 2: Regenerate board.md

**Run unless `--quick`.** Skipping Phase 2 produces a stale Quick Stats line
in Phase 4 output — that's the cost of `--quick`. For default mode, run all
sub-steps before rendering Phase 4.

1. **Regenerate the board** — `python3 scripts/gen-board.py`. The board is
   derived from `work/tasks/` + `work/streams/` + `work/done/` (sections = the
   status enum, counts from the filesystem) — the date header and Quick Stats
   can't drift, and dir↔row reconciliation is automatic (a dir without a row, or
   a row without a dir, cannot arise).
2. **Sync GitHub data** — new items / state changes / completed (skip with `--skip-trackers`).
3. **Surface zombies** — a `work/tasks/<slug>/STATUS.md` whose body asserts
   completion (`done`/✅/`erledigt`) while `status:` ≠ `done` → propose the 3-step
   close (`status: done` → `git mv` to `work/done/YYYY-MM/<slug>/` → regenerate).
   Always propose, never auto-execute; the Stop hook also gates this.
4. **Sync-column for Doing rows** — for each task in the Doing lane, read
   `work/tasks/<slug>/STATUS.md` frontmatter `sync:` block (Phase 2 of
   the Task Sync Routing rollout, see CLAUDE.md § Task Sync Routing).
   Compose a compact marker for the right-most column:

   | STATUS.sync                                                | Marker |
   |---|---|
   | `bridge_only: true`                                        | `— (lokal)` |
   | `github.issues: [188, 189]` (all match Stream B state == `in_progress`) | `#188✓ #189✓` |
   | `github.issues: [189]` but Stream B says issue.state == `done` | `#189!` (drift!) |
   | `github.issues: [...]` not found in Stream B               | `#N ?` (not fetched) |
   | `wiki.path` set and dir exists locally                     | append `wiki✓` |
   | `wiki.path` set, dir missing                               | append `wiki?` |

   Insert column header `| Sync |` if not present; one cell per Doing-row.
   The marker `!` for state drift triggers a Warning line in Phase 4.

   **Skip silently** if Stream B was skipped (`--quick` or `--skip-trackers`)
   — no Stream B data means no drift comparison possible.

5. **Quick Stats** — already current: `gen-board.py` (step 1) recomputed the
   counts (Doing/Review/Backlog/Streams/Done) from the filesystem.

## Phase 3: Log Entry

```
| {TIMESTAMP} | 📋 | bridge | /briefing completed |
```

## Phase 3.5: Cluster-Detection (Render Pre-Pass)

Before rendering individual items in Phase 4, group findings across
all streams by `(category, condition-class)`. When `cluster.size ≥ N`
(default `5`, configurable via `bridge-config.yaml.briefing.cluster_threshold`),
render as **one cluster line** with a Bulk-Action prompt, not as N
separate lines.

Cluster categories and condition-classes:

| Category | Condition (cluster key) | Suggested Bulk-Action |
|---|---|---|
| `application` (if you track an application pipeline) | (`status`, age-bucket) — e.g. (`sent`, `>45d`) | flip → `declined` (decay threshold from your applications standing order) |
| `application` | (`draft`, `>21d unsent`) | flip → `withdrawn` |
| `application` | (`reply`, `>30d`) | flip → `declined` |
| `active-task` | (`status_marker`, idle-bucket) | re-evaluate (no auto-action) |
| `tasks/-drift` | (`drift-type` zombie/orphan) | mv to `done/YYYY-MM/` or restore |
| `github-issue` | (`labels`, `state`, `area`) | close older duplicates |

**Render shape:**

```
── Applications cluster (5 items in `sent` >45d) ──
  acme-staffing, northwind-recruiting, example-agency, globex-talent,
  initech-consulting
  Bulk-Action: all → `declined` (decay threshold reached)
  [y]es / [n]o / [r]eview-each / [d]efer
```

**Rules:**
- Bulk-Action runs NEVER auto-apply. Always interactive: `[y]/[n]/[r]/[d]`.
- `[r]eview-each` falls back to per-item rendering for that cluster.
- `[d]efer` keeps the cluster visible next briefing.
- Cluster-size below threshold → fall through to per-item rendering as
  before.
- Threshold = 5 by default. Lower to 3 for high-noise contexts via
  `bridge-config.yaml.briefing.cluster_threshold`.

**Anti-Pattern:** rendering N≥5 separate lines for the same condition
floods the user with decision-fatigue. One cluster + one decision is
cheaper.

## Phase 4: Terminal Output

**Width: 76 chars.** Box-drawing for focus box. No ANSI colors.

```
╭──────────────────────────────────────────────────────────────────────────╮
│  Week {CW} {Day} {date}  │  Active: {N}/{MAX}  │  {WARNINGS}          │
│  Focus: {ACTIVE_FOCUS}                                                   │
╰──────────────────────────────────────────────────────────────────────────╯

── Warnings ───────────────────────────────────────────────────────────────

  ⚠ {Warning 1}

── Focus today (max 3) ────────────────────────────────────────────────────

  1. {Type} {Task/Ticket} — {next step}
  2. {Type} {Task/Ticket} — {next step}

── {Tracker section title} ────────────────────────────────────────────────

  #{ID}   {Title max 40 chars}                     {Status}   {Assignee}
        + {N} in queue

── QA Queue ({N bugs + N stories}) ────────────────────────────────────────

  [{tracker}] #{ID}  {Title max 35}           {raw_state}        {Assignee}

── Activity (7 days) ──────────────────────────────────────────────────────

  repo-name      {branch}    {sparkline}   {N} commits
  other-repo     main        {sparkline}   {N} commits

── Calendar today ─────────────────────────────────────────────────────────

  Meetings: {X}h  │  Focus time: {Y}h

  {HH:MM}  {Meeting name}                            {Duration}

── Active ({N}) ───────────────────────────────────────────────────────────

  {Ticket}  {Description max 35}     {Context}      {Status-short}

── Imports ({N} files) ────────────────────────────────────────────────────

  {N} .md  ·  {N} .pdf  ·  {N} .xlsx

── Next steps ─────────────────────────────────────────────────────────────

  1. [ ] {from TODO list or focus}
  2. [ ] {from TODO list}
```

**Rendering rules:**
- Titles: max 40 chars, truncate with `..`
- Status: max 12 chars
- Sections with no data: omit entirely (not "No data")
- Sparkline: 7 Unicode blocks, `▁` for 0, `█` for max
- Branch names: max 12 chars, truncate with `..`
- Focus box: always show, even if empty

**Tracker rendering rules (Stream B):**
- 1 provider enabled → section title uses the provider name,
  e.g. `── GitHub (7 open) ──`
- 2+ providers enabled → one subsection per provider using the
  `── Tracker: <Name> (N open) ──` pattern
- QA-category items (items with `category: "qa"` from any provider)
  always render in a dedicated `── QA Queue ──` section below the
  tracker sections. Prefix each row with `[tracker]` so the user sees
  where an item came from. Bugs first, stories second, bold rows
  where `assigned_to_me: true`.
- Done-category items render in a compact "Recently done (last N days)"
  section at the bottom of Stream B. Skip if empty.

**Open-PRs rendering rules (Stream B):**
- Section header counts human and bot PRs separately:
  `── Open PRs (1 human · 24 bot) ──`. Omit the whole section if both are 0.
- List **human** PRs individually (own PRs — author ==
  `assignee_me` — in bold), title max 40 chars, `@author` + age in days.
- Collapse **bot** PRs to a single trailing line with the count, repo
  count, and a copy-paste drill-down command. Never list bot PRs one by one.
- Already archived-filtered upstream (Stream B step 3) — every PR shown is
  on a live, mergeable repo.

## Edge Cases

| Situation | Action |
|-----------|--------|
| Weekend | Warning in focus box, briefing still runs |
| Log spans 2+ cadence buckets | Warning: "log.md covers X {cadence} periods" — bucket per `work.archive_cadence`, never a hardcoded week |
| No tasks/ | Omit active section, suggest queue |
| GitHub unreachable | Warning, local data only (Stream A) |
| No meetings | Omit calendar section |
| Period change | Offer, don't force |
| imports dir has files | Show import section with type breakdown |
| imports dir has transcripts | Offer meeting processing |
| First briefing (empty board) | Short version: focus box + "Board is empty. Create tasks?" |
