# Fixtures

Nothing in this tree talks to a machine. `ssh`, `launchctl`, `systemctl` and
`crontab` are forbidden in the whole suite (a guard in `conftest.py` enforces
it, it does not merely ask). Real services run on the boxes these files
describe, so the suite drives `FakeHost` plus a recording runner instead.

## `corpus/` is the acceptance corpus

Seven declarations, one per structural case found in a live 74 service
inventory. They are the acceptance corpus precisely because they were not
invented: every awkward property in them is a property some real machine
actually has.

| file | the property it exists for |
|---|---|
| `chat-channel.yaml` | `runtime: manual`, `owner: human`: declared so it is visible, never provisioned |
| `contract-review-reminder.yaml` | `kind: oneshot` on the dispatcher, whole file in flow style |
| `calendar-export.yaml` | `kind: interval`: a cadence with no appointment, and an `interpreter` path |
| `public-funnel.yaml` | `owner: foreign` plus a probe carrying an unresolved `<funnel>` placeholder |
| `daily-health-report.yaml` | `delivery_at: 06:30` minus `duration_estimate_min: 20`, so the start is 06:10 |
| `voicememo-notify.yaml` | `watch_paths` AND `every_sec` together, deliberately, not exclusively |
| `voice-channel.yaml` | `retired:` present, and an `expect` written as prose rather than as a pattern |

Two of the seven are unprobeable as written. That is the feature, not a defect
to fix in them: one would resolve a literal hostname if it ran, the other would
be a coin flip dressed as a check. Both must land on `unknown` with the reason
stated.

### One deliberate deviation from the originals

The originals under `workflow/workloads/_tests/valid/` carry live instance
values: a machine name, a personal home directory, customer and product names,
a `com.*` label prefix. This skill is `scope: core` and ships to a public
upstream, so those seven files were copied **structure first**: every id, host,
path, label, mandant and person was replaced with a neutral stand-in, and
nothing else was touched.

Preserved 1:1, because the tests hang off them: the kind / runtime / owner
matrix, flow versus block YAML style per file (`patch_declaration` must refuse
inside a flow mapping), the presence and absence of every optional block, the
06:30 / 20 minute pair, the `watch_paths` plus `every_sec` pair, the unresolved
`<funnel>` placeholder, the prose `expect`, and the `retired:` block.

All seven still validate against the real
`workflow/workloads/_schema.yaml`, which is the check that the copy stayed
faithful. `test_acceptance.py` asserts that no instance literal has crept back
into any file of this skill, fixtures included.

The uid in every canned output is `4242`, never the number a real box uses.
A hardcoded uid therefore fails the tests instead of passing them by accident.

## The other folders

| folder | what it holds |
|---|---|
| `invalid/` | negative controls. Each asserts an error **code** and a message substring, so a rejection for the wrong reason still fails |
| `derived/` | extra declarations for one scar each: midnight crossing, an inexpressible recurrence, a foreign timezone, a linux timer, a cron block, an elevated daemon, `check_ref` resolution, a patchable block style twin, umlauts |
| `hosts/` | stand ins for `infra/remotes/<host>.yaml`: macOS, linux, unreachable, and a platform no backend supports |
| `checks/` | two groups sharing one bare check id, which is what makes `check_ref: disk-free` ambiguous rather than resolvable |
| `outputs/` | canned stdout of the read only calls. Shaped after real output, including the tab indentation `launchctl print` produces |
| `golden/` | per backend byte goldens. Empty until a human reviews the first render: `WORKLOAD_UPDATE_GOLDEN=1` writes them, and a golden that was never reviewed must not be born green |
