---
summary: "Machine-global capability registry (~/.bridge-capabilities/<type>.yaml) — lets one Bridge instance declare that a capability (a transcription worker, a backup pipeline, a remote fleet) exists on this machine and how to reach it, so a SIBLING instance can discover it without reading the first instance's own files. Data isolation stays intact; only capability isolation is lifted, and only for what an instance opts to publish."
type: guide
last_updated: 2026-08-14
related:
  - multi-instance.md
  - workspaces.md
  - transcription-worker.md
  - ../skills/workspace/references/model.md
  - ../skills/debrief/SKILL.md
  - ../skills/meeting-transcription/SKILL.md
  - ../scripts/capability_registry.py
  - ../scripts/lib/registry_io.py
---

# Capability Registry

`docs/multi-instance.md` is right that a Bridge instance must never read another
instance's files — that is the rule that keeps two customer bridges from seeing
each other's data. But the same rule, applied uniformly, also hides **shared
infrastructure**: a transcription worker, a backup pipeline, a remote fleet, a
voice library — things several instances on one machine routinely share. A
fresh instance has no way to know any of it exists, because knowing would mean
reading a sibling's files.

**Data isolation and capability isolation are not the same thing.** This
registry gives capability its own, deliberately narrow, opt-in channel —
without weakening data isolation anywhere.

> **Not to be confused with:** [`docs/multi-instance.md` § Capability
> Isolation](multi-instance.md#capability-isolation) uses "capability" in a
> different sense — whether a Bridge instance's own **skills** execute
> correctly (the `~/.claude/skills` shadowing trap), not whether shared
> **infrastructure** is reachable. This document is about the latter.

> **Origin.** This was born from a concrete incident: an instance ran for a
> day unaware a working transcription worker was reachable on the same
> machine, serving two sibling instances in production (issue #108). Three
> links were broken — onboarding recorded no ledger of what it offered
> (#106), `/debrief`'s fail-soft rule couldn't distinguish "no worker" from
> "worker not registered here" (#105), and there was no channel for a
> sibling to declare a shared capability at all (#107, this document). All
> three needed fixing together; this is the "what all three would read" piece.

## What belongs in it, and what never does

A registry entry may say: **a capability exists, and how to reach it.**

It must **never** say: context names, speakers, customer data, or a path into
another instance's repo. The registry answers *"can this machine
transcribe?"* — never *"for whom, or about what?"*.

This is enforced **mechanically**, not left to a human to remember: the
writer (`scripts/capability_registry.py`) accepts a closed, hardcoded field
allowlist (`ALLOWED_ENTRY_FIELDS`) and there is no code path — CLI or library
— that can smuggle an unlisted field into an entry. See § The entry schema
below.

## Why a new, separate namespace — not an extension of `~/.workspaces/`

`skills/workspace/` already proved the shape this needed: a machine-global,
multi-writer registry (`~/.workspaces/workspaces.json`) with an advisory-lock
+ atomic-replace protocol, standing next to a repo-local materialization layer
— see [`skills/workspace/references/model.md`](../skills/workspace/references/model.md)
§ "The multi-writer protocol" for the exact six-step mechanics this registry
reuses.

But **workspaces are a different concept from capabilities**: a workspace
records *project identity* — which repos belong to which named project, for
whichever tool cares. A capability records *machine fact* — does infra X exist
here, independent of any project. Cramming both into `workspaces.json` would
conflate two unrelated schemas and force every capability consumer to parse
workspace rows it doesn't care about (and vice versa for workspace consumers).
So this is:

- its **own directory**, `~/.bridge-capabilities/` (`$BRIDGE_CAPABILITIES_DIR`
  overrides, same resolution shape as `$WORKSPACES_DIR`) — not a slice inside
  `~/.workspaces/`.
- its **own writer**, `scripts/capability_registry.py` — not a mode of
  `scripts/workspace_registry.py`.
- **single-owner**, unlike the workspace registry: only Bridge instances write
  it (no external tool conforms to it), so there is no foreign-extension-slice
  preservation, no cross-tool adopt/merge logic, no path-alias matching. What
  IS shared with the workspace registry is the **protocol shape** — advisory
  lock → whole-file read → modify in memory → atomic replace → unlock — not
  the file, not the schema, not the identity-matching rules.

The shared, registry-agnostic **mechanics** (the lock, the atomic write, the
version coercion) live in [`scripts/lib/registry_io.py`](../scripts/lib/registry_io.py)
and are used by `capability_registry.py`. `workspace_registry.py` was
deliberately **not** refactored onto the same module: its test harness
(`scripts/tests/test-workspace-registry.sh`) dynamically loads copies of the
file from arbitrary paths and mutates specific source-text anchors to prove
its "teeth" tests have bite — both assumptions (the file is self-contained;
the anchors live in that exact file) break under a shared import. Forcing the
extraction there would have silently turned two of its 21 test sections into
false-positive passes. `capability_registry.py` has no such constraint, so it
imports the shared lib cleanly.

## Layout — one file per capability TYPE, a list of entries

```
~/.bridge-capabilities/
  transcription.yaml      # one file per capability TYPE (not per instance)
  transcription.yaml.lock # advisory lock, held only during a write
  backups.yaml             # (a future provider — not built here, see below)
```

```yaml
# ~/.bridge-capabilities/transcription.yaml
version: 1
entries:
  - provider: meeting-transcription
    host: worker-host                              # SSH/Tailscale alias (remote mode); omitted for local
    launchd_label: com.openbridge.transcribe-worker
    contexts_dir: ~/transcribe-pipeline/contexts
    registered_by: acme-consulting                  # a NAME only — never a path into that instance
    registered_at: 2026-08-14T09:15:00Z
  - provider: meeting-transcription                 # a SECOND instance can register
    registered_by: acme-internal-ops                # the SAME provider without collision —
    registered_at: 2026-08-01T11:02:00Z              # keyed by (provider, registered_by)
```

One file per TYPE holding a **list**, so multiple instances can each register
the same capability type without file collisions beyond the usual lock — and
a consumer that only cares about `transcription` never has to parse anything
about `backups`.

### The entry schema

| Field | Required | Meaning |
|---|---|---|
| `provider` | yes | which skill/tool serves this capability (e.g. `meeting-transcription`) |
| `registered_by` | yes | the **name** of the instance that published this entry (`identity.name`) — never a path |
| `registered_at` | yes | ISO-8601 UTC timestamp of the last publish — a staleness signal, not a liveness guarantee (see below) |
| `host` | no | SSH/Tailscale alias of the machine serving it — omitted when placement is `local` (same machine, no alias) |
| `launchd_label` | no | the service's launchd/systemd unit name |
| `contexts_dir` | no | where the provider's runtime config lives — a path **on the host serving the capability**, never a path into the publishing instance's own repo |

This set is closed (`capability_registry.ALLOWED_ENTRY_FIELDS`). Adding a
field for a new capability type is a deliberate code change in
`capability_registry.py`, not a runtime-open door — see § Extending below.

## Read path is fail-open, write path is fail-closed

**Reading** a missing file, or a missing directory, is not an error — it
means "nothing declared here." This is deliberate: a declaration is not a
scan. Reading something a sibling instance chose to publish, for exactly this
purpose, violates no privacy boundary — unlike scanning the machine for
evidence, which `discovery.mode: confined` correctly refuses to do by
default. No permission gate belongs on a registry read.

**Writing** follows the opposite discipline, same as the workspace registry:
an unparseable file, a missing/non-numeric `version`, or a `version` newer
than this writer understands all **refuse the write**, bytes left exactly as
found — never guessed, never silently reset.

## Who writes an entry — opt-in, checked on every run, default OFF

Two designs were on the table: a human-run `register` verb, or an automatic
check wired into whatever the providing skill already does regularly. The
issue that motivated this (#107) worried, correctly, that auto-publishing on
every deploy risks registering something the user considers private to one
instance — so **default OFF** is not negotiable either way. The question was
only how the *opt-in* is remembered once given.

**A manual verb is exactly the failure mode that caused the original
incident** — a human has to remember an extra command, and eventually
doesn't. So the design here is a **config flag, checked automatically every
time the providing skill runs its normal sync operation**:

```yaml
# bridge-config.yaml
integrations:
  transcription:
    share_capability: false   # publish this worker's reachability to the
                               # registry so a sibling instance can discover
                               # it. Opt-in, default off.
```

`skills/meeting-transcription/scripts/debrief_sync.sh` checks this flag at
the end of every successful `pull` / `push` (not a one-off deploy step — the
sync script is what actually runs repeatedly, so this is the true "every run"
touchpoint in that skill, not a manual provisioning command that would go
stale between runs):

- `share_capability: true` → publish/refresh this instance's entry
  (`capability_registry.py publish <type> --provider … --registered-by …`).
  Refreshing on every real sync keeps `registered_at` meaningfully current —
  it only stays fresh while the worker is actually being used successfully,
  which is a cheap, honest proxy for "still alive" without ever probing.
- `share_capability: false` (or unset — the default) → remove this
  instance's own prior entry, if any (`capability_registry.py remove <type>
  --registered-by … --provider …`). This is a **safe no-op** when nothing was
  ever published. It also means flipping the flag back off **self-heals** on
  the very next sync — no separate teardown step to remember.

Removal is always scoped to `registered_by == <this instance's name>` and
**never** touches another instance's entries — the write API has no call
shape that could.

## No liveness probe — staleness is named honestly instead

A registry entry can go stale (the worker was decommissioned, the machine was
reimaged) with no removal ever happening — the "self-heal on next sync"
above only fires if that instance ever runs its sync script again. Actively
probing every registered capability to check it's still alive would turn a
**read** into a **scan** — precisely what this design avoids. So a consumer
that surfaces a registry entry names its age honestly instead of asserting it
as current fact: an entry with `registered_at` **older than ~90 days** gets
phrasing like *"last confirmed {date} by {registered_by} — verify it's still
running"*, not a flat "a worker exists." A registry that lies is worse than
one that is empty; naming the uncertainty is the cheap alternative to a
liveness story this design deliberately doesn't build.

## Consumers

- **`skills/debrief/SKILL.md`** § Transcription pipeline splits its fail-soft
  rule: `integrations.transcription.enabled: false` stays silent (a
  deliberate no); the block being **absent** runs two cheap checks — local
  topology/sync-script presence, and this registry — before deciding whether
  to say one line and continue, or stay silent. See that section for the
  exact wording, including the staleness phrasing above.
- **`/bridge-onboard`** could read this registry in a future Suggestions pass
  under `discovery.mode: confined` (a declaration is not a scan, so this
  would not violate the confined boundary) — not built in this change; see
  issue #108's open questions.

## Extending to a new capability type

Nothing here is transcription-specific except the one wired-up provider. A
future capability (a shared backup pipeline, a shared remote fleet) follows
the same recipe:

1. Pick a `<capability-type>` slug (`[a-z][a-z0-9_-]*`) — its own file,
   `~/.bridge-capabilities/<type>.yaml`.
2. Decide what "exists + how to reach it" means for that capability, and
   extend `ALLOWED_ENTRY_FIELDS` in `scripts/capability_registry.py` with
   exactly those fields — reachability only, still never content or identity
   of who's using it.
3. Wire the providing skill's own repeat-invocation point (whatever it is —
   a sync script, a health check, a scheduled job) to call `publish` /
   `remove` the same way `debrief_sync.sh` does, gated by its own
   `share_capability`-shaped opt-in flag, default off.
4. Wire the consuming skill's fail-soft rule the same way `/debrief`'s is
   split above.

This document describes the pattern from what was actually built for
transcription — it does not speculatively wire backups or remote fleet; that
is future work following the same recipe.

## State-file locations

| Artifact | Location | Tier |
|---|---|---|
| Registry file (per type) | `~/.bridge-capabilities/<type>.yaml` | machine-global, outside any repo |
| Lock | `~/.bridge-capabilities/<type>.yaml.lock` | machine-global, advisory |
| Writer / reader engine | [`scripts/capability_registry.py`](../scripts/capability_registry.py) | core |
| Shared lock/atomic-write mechanics | [`scripts/lib/registry_io.py`](../scripts/lib/registry_io.py) | core |
| Opt-in config key | `bridge-config.yaml` → `integrations.<provider>.share_capability` | scope: user (per instance) |

## The command surface

```
capability_registry.py [--registry-dir D] path
capability_registry.py [--registry-dir D] list-types
capability_registry.py [--registry-dir D] read <type>
capability_registry.py [--registry-dir D] list <type>
capability_registry.py [--registry-dir D] publish <type> --provider P --registered-by NAME
    [--host H] [--launchd-label L] [--contexts-dir D]
capability_registry.py [--registry-dir D] remove <type> --registered-by NAME [--provider P]
```

`--registry-dir` (or `$BRIDGE_CAPABILITIES_DIR`) overrides the default
`~/.bridge-capabilities/` — every test pins it to an isolated temp directory,
the real directory is never touched by a test run.

## Verification

[`scripts/tests/test-capability-registry.sh`](../scripts/tests/test-capability-registry.sh)
covers fail-open reads, upsert-by-`(provider, registered_by)` publish, the
multi-instance no-collision case, `registered_by`-scoped remove (including the
never-touch-another-instance's-entry guarantee), the closed entry schema (both
the explicit validator and the structural fact that `publish()`'s fixed
signature has no parameter to smuggle an unknown field through), fail-closed
version/anomaly handling, and a parallel-writer lock check.
[`skills/meeting-transcription/tests/test-transport-modes.sh`](../skills/meeting-transcription/tests/test-transport-modes.sh)
§ 6 covers the `share_capability` wiring end-to-end through `debrief_sync.sh`
— opt-in publish, default-off silence, and the self-heal-on-false removal.

## Related

- [Multi-instance](multi-instance.md) — the data-isolation rule this registry
  deliberately does not weaken; read it first for why the boundary exists.
- [Workspaces](workspaces.md) — the prior art this reuses the protocol shape
  from, and why capabilities are a separate concept from project identity.
- [Transcription worker](transcription-worker.md) — the reference provider
  wired up to this registry (`share_capability`, the fail-soft split it feeds).
- [`skills/workspace/references/model.md`](../skills/workspace/references/model.md)
  § The multi-writer protocol — the six-step lock/read/modify/atomic-replace/
  unlock mechanics both registries follow.
