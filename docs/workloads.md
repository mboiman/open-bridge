---
summary: "Workloads: one declared run on one machine, in one file. The four part contract (placement, schedule, execution, response) plus a reconcile probe, why there is deliberately no status field, and how ownership is proved on both sides."
type: guide
last_updated: 2026-08-27
related:
  - rules/deploy-reconciliation.md
  - rules/visual-output.md
  - docs/structure.md
  - docs/remotes.md
  - docs/channels.md
---

# Workloads

A **workload** is one thing that runs on one machine: a scheduled report, an
interval poller, a daemon, an inbound agent, a path watcher, or a one-shot. It
lives as a single declaration in `workflow/workloads/<id>.yaml`, and the
`workload` skill renders the unit file from it, provisions it, and reconciles
the declaration against the live service manager.

The declaration is the source of truth. What sits on the machine is an artifact
and may be rebuilt from the declaration at any time.

## Why this exists

The state it grew out of is worth naming, because it is the normal state of a
machine that has been used for a few years. On one host: 74 service entries in
an inventory that describes runs but creates none; a scheduling skill whose
registry file did not exist; a calendar whose eight entries had all never
fired; and an executor whose registry lived outside the repository entirely.

Five places to register a run, and **no ownership anywhere.** Of the things
running on that machine, nothing recorded which the Bridge had created, which a
person had created by hand, and which a tool had left behind. The visible
consequences were eleven backup copies of one registry file in five naming
schemes, a `launchd.broken-<date>` directory nobody had touched in months, and
1428 log files with no rotation.

`owner` is the field none of the five had. Everything else in the contract
below is the union of what already existed, scattered.

## The contract, in four parts

```yaml
schema_version: 1
scope: user                    # core | org | personal | user, the routing tier
id: daily-health-report        # stable slug, matches the filename
purpose: "Daily operations report, due before the morning stand-up"

placement:                     # 1. WHERE IT SITS
  host: host-a                 # a slug under infra/remotes/, or `local`
  kind: recurring              # recurring | interval | daemon | watch | agent | oneshot
  runtime: launchd             # launchd | launchd-system | systemd | cron | dispatcher | manual | external
  owner: bridge                # bridge | human | foreign
  interpreter: /usr/bin/python3

schedule:                      # 2. WHEN IT FIRES
  rrule: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
  delivery_at: "06:30"         # DELIVERY, not start
  duration_estimate_min: 20    # start = delivery_at minus this
  timezone: Europe/Berlin      # a zone, never a fixed offset

execution:                     # 3. HOW IT RUNS
  command: ["/opt/example/bin/report.sh"]
  timeout_sec: 600
  isolation: process-group
  single_flight: true
  on_timeout: report

response:                      # 4. HOW IT ANSWERS
  evidence: log-trace          # exit-code | log-trace | delivery-receipt
  recipients: [{mandant: example-team, person: first_person}]
  notify_on: [failure, timeout, missing]

reconcile:                     # optional: how you know it is still there
  probe: "launchctl print gui/501/com.example.dispatcher"
  expect: "state = running"
```

`kind` and `runtime` are separate on purpose. An inventory that conflates them
into one `type:` field ends up with one word covering both a 06:30 weekday
report and a poller that fires every 900 seconds. Those are different things:
one has appointments, the other only a cadence. Splitting them is what lets a
calendar view draw appointment ticks for the first and a cadence band for the
second without inventing times that never existed.

## There is no `status:` field

That is the reason this schema exists, not an omission. A declared status is
never the truth; the service manager is
([`rules/deploy-reconciliation.md`](../rules/deploy-reconciliation.md)). State
comes from `reconcile` asking the live source.

The inventory that preceded this proves the point: 52 of its 74 entries carried
no status at all, and the ones that did had drifted. `additionalProperties:
false` at the top level is what keeps the field from coming back through the
side door, and it is the single heaviest rule in the schema.

## Ownership takes two signals, never one

A stamp on the machine and a marker inside the artifact. A stamp without a
marker is drift, and so is the reverse. Two blind procedures that can only ever
agree with each other would prove nothing.

## The hard rules

- **A deadline, evidence and `owner: bridge`** are mandatory for every kind the
  Bridge both owns and executes. A run without a deadline once held a machine
  for three and a half hours.
- **Recipients are references** (`mandant:` / `person:`), never plaintext
  addresses: a declaration is a tracked file and travels with the scope router.
- **`execution.env` carries locators, never values** (`keychain://…`), and the
  locator is what reaches the unit file. Nothing resolves it on the way: a
  resolved secret would be written to disk. The program resolves its own at run
  time.
- **The program keeps its name.** No version segment in
  `placement.interpreter`: a versioned path is deleted by the upgrade that
  creates its successor, and on macOS a privacy grant is keyed on the literal
  path, so it is orphaned in silence. A declared `placement.privacy_grants`
  additionally refuses an interpreter the whole machine shares, because a grant
  is issued to a PATH and would then belong to every program at it.
- **Running one by hand proves nothing about the job.** An interactive or ssh
  session has its own identity and its own grants; the honest probe runs in the
  service manager's context.

## Where the files live

| Path | Tier | What |
|---|---|---|
| `workflow/workloads/_schema.yaml` | CORE | The contract |
| `workflow/workloads/_template.yaml` | CORE | The starting point for a new declaration |
| `workflow/workloads/_tests/` | CORE | The contract's own regression suite |
| `workflow/workloads/<id>.yaml` | USER | Your declarations |
| `skills/workload/` | CORE | The engine and the skill |

A declaration names a concrete host, concrete paths and concrete recipients, so
it is `user` (or `org`, or `personal`) and never travels upward. The contract
and the engine are generic and do.

## Proving the contract

```bash
bash workflow/workloads/_tests/run.sh            # every rule still enforced
bash workflow/workloads/_tests/run.sh --mutate   # and every control still bites
bash skills/workload/run-tests.sh                # the engine
```

The second one is the unusual half and the reason the first is worth trusting.
It softens one rule in a scratch copy of the schema and requires that exactly
one negative control goes hollow. A control that stays red under its own
softening was never testing that rule. Two needles had gone hollow before
anyone noticed: one matched nothing after a rule was restructured, one matched
twice after a second spelling of the same pattern appeared.

Every run ends with a written list of what is **not** fully covered, so the
green above it cannot be read as completeness.
