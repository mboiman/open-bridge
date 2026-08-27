# provision, adopt, retire: the operator flow

These are the three commands that change a machine. Everything else in this
skill reads.

The shape is always the same: **observe, plan, print, apply, verify.** `plan` is
pure, so what will happen can be shown to a person and then relied on. `apply`
runs under a per-id lock and never reports success without a verify that passed
at the live object.

## The plan table

`plan` decides from the declaration, the rendered artifact and what the machine
actually answered. It never derives anything from a declared status field.

| What was observed | action | reason code |
|---|---|---|
| nothing there | `create` | `nothing-provisioned` |
| stamp digest equals the artifact digest and the files match | `noop` | `already-in-sync` |
| stamp digest differs | `replace` | `artifact-drift` |
| files there, no stamp, no marker | `refuse` | `collision-unstamped` |
| the marker names a different workload | `refuse` | `collision-foreign-workload` |
| our marker, byte identical, but no stamp | `refuse` | `marker-without-stamp` |
| a file changed since it was stamped | `refuse` | `foreign-edit` |
| the unit is on the persistent off-list | `refuse` | `disabled-refused` |
| the declaration is retired | `refuse` | `retired-declaration` |
| the declaration fails the invariant gate | `refuse` | `invalid-declaration` |
| the owner is not `bridge` | `refuse` | `not-owned` |
| the runtime cannot carry a demanded guarantee | `refuse` | `degraded-backend` |
| the host did not answer | `refuse` | `host-unreachable` |
| the steps need elevation | `manual` | `elevation-required` |

That `noop` row is the whole reason this may be automated at all: rendering is
pure and carries no timestamp, so a second run produces byte-identical files and
idempotence is a property rather than a hope.

## Every refusal, and what answers it

| code | What it means | Remedy |
|---|---|---|
| `collision-unstamped` | something with this name exists and nothing says it is ours | look at it. `adopt` if it is ours, rename one of the two if not. It is never overwritten |
| `collision-foreign-workload` | the unit carries another workload's ownership marker | two declarations claim one unit; fix the ids |
| `marker-without-stamp` | provably ours, but the ownership record is gone | `adopt`, which writes only the record and restarts nothing |
| `foreign-edit` | a file was changed on the machine after it was stamped | read the difference first. `--force` overwrites it |
| `disabled-refused` | somebody switched this off persistently | leave it off, or lift it deliberately with `--enable`. Never automatically: for one real declaration a start would be a security incident |
| `retired-declaration` | the declaration carries a `retired:` block | remove the block deliberately, or make a new declaration |
| `invalid-declaration` | the declaration does not pass `validate` (the cross field gate: a deadline, a command, evidence, the kind/schedule matrix) | fix the key the refusal names. `validate` is asked here too, not only by the `validate` command, because the layer that ACTS is where it has to hold: a declaration without `execution.timeout_sec` otherwise became a run with no deadline on a machine |
| `unconfirmed-stop` | `retire` was called without an explicit confirmation | confirm the retirement. Not being told yes is not a yes, and this is the only command that stops a running service |
| `not-owned` | `owner` is `human` or `foreign` | nothing to do. It is documented so it is visible, and never touched |
| `degraded-backend` | the runtime cannot answer for something the declaration demands | move it to a runtime that can, or accept the shortfall with `--accept-degraded`, which still says so in the output |
| `host-unreachable` | the machine did not answer | not knowing what is there is not the same as nothing being there. Fix the connection and run again |
| `not-provisionable` | runtime `manual` or `external` | by design. The declaration makes the run visible, it does not create it |
| `elevation-required` | the plan needs root | the steps are printed. See below |
| `symlinked-unit-path` | the unit path is not its own physical path | point the declaration at the real directory. A service manager refuses a symlinked unit path outright |
| `lock-held` | another session holds this id | wait, or find out what that session is doing |
| `still-running` | after the disable, the probe still answers as healthy | it did not stop. Nothing was written back |
| `stop-unproven` | the stop could not be evidenced | an unproven stop is not a stop. Nothing was written back |
| `unsupported-runtime` / `unsupported-kind` / `unsupported-recurrence` / `unsupported-timezone` | the backend cannot express it | named in the message, with what it can express |

## The elevation path

There is no `sudo` anywhere in this skill. A plan that needs root comes back as
action `manual` with the steps written out, and a person runs them. A later
`provision` run then observes the result and verifies it like any other.

That is not caution for its own sake: the machines this runs against carry live
services, and a silent escalation there has no undo.

## Ownership: two independent signals

A unit belongs to the Bridge when both of these say so.

1. **A stamp file** on the machine, one per workload under the configured stamp
   directory, expanded on the host through `$HOME` and never a literal path. It
   records the workload id, the declaration path and digest, the artifact
   digest, the runtime, the unit reference, the file list, when it was
   provisioned, and whether it was adopted. It carries no user name and no
   secret.
2. **A marker inside the artifact**, in the format's own idiom: environment
   entries for launchd and systemd, a delimited comment block for cron, a field
   in the registry entry for a dispatcher.

`reconcile` can therefore answer "is this ours" from the machine alone. A stamp
without a marker, or a marker without a stamp, is drift and is reported as such.
Two procedures that can only ever agree would prove nothing.

**The two digests are different currencies and are never compared against each
other.** The marker carries the digest of the DECLARATION, because it sits inside
the very file the artifact digest covers and a value cannot contain its own hash.
The stamp records both: its declaration digest is what the marker is compared
against, its artifact digest is what the files are compared against. Comparing
the marker to the artifact digest makes `in_sync` unreachable and reports every
correctly provisioned run as drifted forever, with a repair hint that reproduces
the same state.

## adopt

The zero downtime entry ramp for something that already exists by hand.

`adopt` verifies a live unit matching the declaration's unit reference, then
writes **only** the ownership stamp, recording the digest of what is actually
there. It rewrites nothing and restarts nothing. Afterwards `reconcile` compares
against the adopted digest, so a hand made unit becomes owned without a second of
downtime.

### The unit is named what it is named

A unit somebody made by hand almost never carries this instance's label prefix,
and that is the whole point of adopting it. Declare the prefix it does carry:

```yaml
placement:
  label_prefix: org.example.scheduler   # the unit is org.example.scheduler.<id>
```

The declaration **id stays the tail** of the name. That is not cosmetic: the
inventory matches on it, the ownership stamp is keyed on it, the trace file and
the guard script are named from it. A whole free-form label would break the
relationship for all four at once and do it silently, which is why the knob is a
prefix and not a name. Measured against a machine carrying 55 hand made units,
every single one decomposed into `<prefix>.<id>[.<appointment>]`, so a prefix is
enough.

Two declarations must not resolve to one name. `validate` refuses that over
**all** declarations, even when `--id` names one, because the loser of a
collision does not fail: it silently claims the other one's unit, stamp and
trace.

If `adopt` answers `nothing-to-adopt`, read the unit reference it names before
concluding the unit is gone. The common cause is a name this declaration cannot
yet form, not an absent service.

## retire

Switching something off means `disable` **plus the reason**, never a file rename.
Only the persistent disable survives a reboot, and a renamed file has lost the
why by the time anybody asks.

The order matters and is asserted:

1. stop it now (`bootout` or the backend's equivalent),
2. the persistent disable, carrying the reason,
3. **prove it stopped** at the live source,
4. write the `retired:` block into the declaration,
5. mark the stamp retired,
6. remove the artifact files, unless `--keep-artifact`.

Step 3 is a gate, not a formality, and it is judged by the declaration's own
`expect`, not by an exit code and not by a list of service-manager phrases. A
probe that still **passes** means the thing still answers the way a healthy one
does, so it did not stop. A probe that cannot be evaluated is not proof either.
Both stop the sequence before step 4, so the repository can never claim retired
while the machine is still serving.

A reason shorter than eight characters is refused before anything is touched.

So is a retirement nobody confirmed. Two signals decide whether the sequence
above runs at all, and neither is the other's negation: an explicit dry run
stops nothing, and neither does a call that carries no confirmation. A caller
that owns one boolean and derives the other has exactly one place to get it
wrong, and this is the one command where getting it wrong stops a live service.

From the command line that reads:

| what you type | what happens | exit |
|---|---|---|
| `retire <id> --reason ...` | nothing. Refused as `unconfirmed-stop`, with the reason | `3` |
| `retire <id> --reason ... --dry-run` | the preview: what it would do, and that it stopped nothing | `1` |
| `retire <id> --reason ... --yes --dry-run` | the same preview. The dry run wins, and this is the line that used to boot out a live unit | `1` |
| `retire <id> --reason ... --yes` | the sequence above, verified at the live object | `0` |

`retire` is the one command whose bare form does not preview but refuses:
`provision <id>` without `--yes` prints its preflight and says to rerun with
`--yes`, while `retire <id>` stops at the bolt. The asymmetry is deliberate — the
preview of a stop is a machine call sequence nobody asked for — and it is why
`--dry-run` exists on `retire` at all.

## Moving an existing service into the repo

One at a time, and the old one stays until the new one is proven. Six steps:

1. **Declare it**, without provisioning. The old service keeps running unchanged.
2. **Provision the new one in parallel**, under its own id, so nothing collides.
3. **Run both.** Say beforehand that the recipient will get two of everything
   during this window, or it reads as a fault.
4. **Accept it.** The new path must have delivered **twice**, with evidence. For
   a weekday report that is two working days, not an afternoon.
5. **Switch the old one off**: the persistent disable plus the reason, never a
   rename.
6. **Prove exactly one path is left.** Otherwise the result of unifying is
   duplicate delivery.

The way back is to re-enable the old path and `retire` the new one. Until step 5
has happened the way back costs nothing, which is why the order is this one.

Two things to say out loud during step 1. A job that used to name its START time
now names its DELIVERY time, so it will deliver earlier than it does today. And
recipients become references to a mandant or a person, so the addresses move out
of the job and into one place.

## The two deadlines, which are not the same deadline

- **The control plane deadline** is this skill's own. Every step, every ssh call
  and every probe runs under it, in a new session, and the whole process group is
  killed when it expires. An expiry raises; it never becomes a return code.
- **The run plane deadline** is `execution.timeout_sec` and belongs to the
  artifact. systemd carries it natively; launchd and cron get it from the
  generated guard script.

The skill never enforces the run deadline itself, and the guard never enforces
the skill's.
