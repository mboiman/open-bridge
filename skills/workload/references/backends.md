# backends: what carries a run, and what it promises

A backend turns one declaration into the exact bytes that belong on a machine,
plus the step plans that install, replace, disable, remove, inspect and enumerate
it. Everything in the package is pure: no filesystem, no subprocess, no clock.
The same declaration renders the same bytes forever, which is what makes a second
provision run a no-op instead of a hope.

`render.py` performs exactly one dictionary lookup and contains no comparison
against the name of a runtime or a platform. Every capability is **data** the
backend declares.

## The capability table

| runtime | platforms | kinds | guarantees natively | refuses |
|---|---|---|---|---|
| `launchd` | macos | recurring, interval, daemon, watch, agent | single flight | `oneshot` (points at the dispatcher); a declared timezone the host does not run in; an untranslatable recurrence |
| `launchd-system` | macos | as above | single flight | as above, and every plan is `manual`: it needs elevation, which this skill never takes |
| `systemd` | linux | recurring, interval, daemon, watch, agent | deadline, process group kill, single flight, missing detection | `oneshot`; an untranslatable recurrence |
| `cron` | macos, linux | recurring, interval | nothing | `daemon`, `watch`, `oneshot` |
| `dispatcher` | macos, linux | every kind | whatever the configuration says it does, empty by default | renders nothing at all when no registry is configured |
| `manual` | every | every kind | nothing | rendering, always: `not-provisionable` |
| `external` | every | every kind | nothing | rendering, always: `not-provisionable` |

## The four guarantees, and why each one is a currency

| guarantee | The scar behind it |
|---|---|
| `deadline` | a run without one held a machine for three and a half hours and nobody noticed until the next morning |
| `process_group_kill` | ending only the direct child leaves a grandchild holding the output pipe, and the cleanup after it blocks forever, which is the exact shape of that hang |
| `single_flight` | one job lost 53 of 181 runs to overlapping starts, in silence |
| `missing_detection` | a run that never happened is the failure nobody sees; a trace line per run is what makes its absence provable |

`model.required_guarantees()` derives from the declaration what the runtime must
supply: a `timeout_sec` demands `deadline`, `isolation: process-group` demands
`process_group_kill`, `single_flight: true` demands `single_flight`, and
`missing` in `notify_on` demands `missing_detection`.

`render` subtracts what the backend promises natively and asks the guard script
to supply the difference. Whatever nothing can supply is recorded in the
artifact's notes and refused as `degraded-backend` unless it is accepted
deliberately. It is named rather than swallowed: a guarantee nobody answers for
is the thing an operator has to know before the run matters.

## Per backend, the parts that are load bearing

### launchd

One implementation, two instances parameterised from data: the user domain
(`gui/<uid>`, agents under the user's own launch directory) and the system domain
(daemons under the system directory, `requires_elevation`).

- The domain target carries the **uid discovered on the host**. A hard-coded
  number points a whole plan at somebody else's session.
- A replace is `bootout`, then the new bytes, then `bootstrap`. **Never
  `kickstart`**: kickstart restarts what is loaded, it does not reload a unit
  whose file changed, so a schedule change silently would not take. A test
  asserts the string does not occur in a replace plan.
- `bootstrap` needs the unit file's **physical** path. A launch directory that is
  a symlink into a synced folder is a real configuration, and bootstrap on a
  symlinked path fails outright. It is resolved with `cd` plus `pwd -P`, because
  BSD `readlink -f` support is not universal.
- `RunAtLoad` stays false for recurring and interval. A bootstrap at 15:00 must
  not fire the 06:30 report.
- A `watch` emits `WatchPaths` **and** `StartInterval` when a cadence is declared
  beside it. That is not a contradiction: a path watcher can fire before a file
  has finished arriving, so the cadence is the fallback.
- The persistent off-list is a **separate read** (`print-disabled` against the
  domain), because the per-unit call does not carry it. Without that read the
  refusal protecting a deliberately stopped unit cannot fire at all.

### systemd

A `.service` plus a `.timer`, or a `.path` for a watcher, under the user unit
directory. It is the only backend that promises all four things by itself, and
each promise is written into the unit rather than asserted:

- `RuntimeMaxSec=` is the deadline,
- `KillMode=control-group` ends the whole cgroup and not merely the process that
  was started,
- a timer does not trigger a service that is still active,
- the journal records every start and exit with a timestamp, so "it did not run"
  is a question with an answer here. On launchd it is not: that run counter
  resets and takes the evidence with it.

Because nothing is missing, no guard script is attached.

The marker sits in two places: `Environment=` in `[Service]`, which is readable
back through `systemctl show`, and `X-BridgeWorkload=` in `[Unit]` for a human
reading the file.

### cron

One delimited block inside the user's crontab, and only that block is ever
rewritten, so a hand-written line beside it survives every pass. `%` is escaped,
because an unescaped one ends the command and feeds the rest to stdin.

cron guarantees nothing, so a cron workload is **always** wrapped. It also has no
login PATH, which is why the guard names one explicitly: the difference between a
run and a line in a log nobody reads.

### dispatcher

One entry in the registry named by configuration. Its guarantees are **read from
configuration** and default to empty, so a dispatcher that grows a deadline and a
group kill declares them in config and no code changes here.

Until it does, a workload demanding those lands in the `degraded-backend`
refusal. Unconfigured, the backend refuses clearly rather than writing an entry
into nowhere.

### manual and external

They are backends like any other, which is the whole point. A run started by hand
in a terminal, or one a third-party platform manages, still belongs in the
inventory: `reconcile` has to see it, and the two kinds that can never be created
from a declaration must not become an `if` in the middle of the state machine.

`render()` raises `not-provisionable`. `default_probe` and `discover_steps` keep
working, so a run nobody provisions can still be observed. They classify as
`observed` and are never touched.

## Recurrence: a restricted subset, never an approximation

Nothing here evaluates an RRULE generally. Each backend translates
`FREQ=DAILY` and `FREQ=WEEKLY` with `BYDAY`, both at `INTERVAL=1`, into its own
idiom (`StartCalendarInterval`, `OnCalendar`) and raises
`unsupported-recurrence` by name for everything else.

A parser that silently understands only `FREQ=DAILY` turns every weekly entry
into a single fire, and the result looks plausible afterwards. General recurrence
evaluation belongs to the dispatcher, which is also why there is no date library
in the dependency list.

`delivery_at` minus `duration_estimate_min` is the start, and the subtraction may
cross midnight: `00:10` minus twenty minutes is `23:50` on the previous day, so
the day shift travels with the weekday and every backend applies it.

## The guard script

Generated for whatever the backend underneath does not promise. POSIX `sh`, never
bash: `/bin/sh` on macOS is not bash and a bashism there fails at 06:10 on a
machine nobody is watching. It does not reach for `timeout(1)` either, because a
stock macOS does not ship it.

- The deadline is a watchdog subshell counting whole seconds, which needs nothing
  but `sh`, `ps` and `sleep`.
- The run is put into a session of its own with `setsid` where that exists,
  and only falls back to the shell's monitor mode where it does not. The fallback
  is not the plan: dash refuses job control without a controlling terminal, which
  is exactly how cron starts things, and on many Linux systems `/bin/sh` **is**
  dash and cron is the backend that is always wrapped. Measured there, the run
  and the guard shared one group, only the direct child could be ended, the
  grandchild kept the pipe and the caller blocked past every deadline.
- The watchdog reads the group back before it kills. Where the run has a group of
  its own it ends it whole; where the guard itself is the group leader it writes
  the record first, because it goes down with it; where the group belongs to
  somebody else it ends only the run and says the shortfall out loud rather than
  hiding it.
- Single flight is an atomic `mkdir` plus a `kill -0` liveness check, because a
  test-then-write is not atomic and a directory whose holder is gone has to be
  reclaimable.
- One trace line per run under `log-trace` or `delivery-receipt` evidence, which
  is what makes an absent run detectable at all. Under `exit-code` evidence it
  writes nothing: a trace there would be a stronger claim than the declaration
  makes.

## Adding a fifth backend

One file plus one line in the registry. `render.py` does not change.

The new module declares four data attributes (`name`, `platforms`, `kinds`,
`guarantees`) and implements the step seams:

```
render(w, h, ctx) -> Artifact
install_steps / replace_steps / disable_steps / uninstall_steps
default_probe
inspect_steps  / parse_inspection(outs, unit_ref)   # everything about ONE unit
disabled_list_steps / parse_disabled(outs, unit_ref) # the persistent off-list
discover_steps / parse_discovery(outs)               # enumerate a machine
```

The three read seams answer different questions on purpose. `inspect_steps` is
what `provision.observe` uses, which is why no service-manager output format is
parsed anywhere outside this package. `disabled_list_steps` is separate because
the per-unit call does not carry the off-list; a backend that cannot answer
returns no steps, and the answer is then `None` (unknown), never `False` (not
disabled). Not knowing whether something was switched off deliberately must never
read as permission to switch it on.

Add the module, add its instance to `BACKENDS`, and give it a row in the golden
tests and in the capability table above.
