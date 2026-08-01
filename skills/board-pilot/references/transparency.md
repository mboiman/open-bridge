---
scope: core
summary: The transparency layer — the two comment streams and why their markers cannot merge, the per-stage narration in the sticky, the byte-0 anchor that defeats the read-back hijack, the engine-writes-evidence contract, and what the PR dossier may claim as REAL vs CLAIM.
type: reference
last_updated: 2026-07-17
related:
  - ../SKILL.md
  - ./operations.md
---

# board-pilot — the transparency layer

The engine posts machine-written text onto an issue that a human then merges on.
Everything here exists to keep that text **honest**: it reports what the engine
*observed*, never what it hopes happened, and it is structurally unable to feed
its own output back to the agent it is evaluating.

Read this before changing `engine/record.py`, `engine/scan.py`, `parse_reject`,
or any stage that writes a comment.

## 1. Two comment streams, one author

The engine authors two kinds of comment on the same issue. They are **not**
interchangeable and must never be folded into one.

| Kind | Marker (**byte 0, line 1, alone**) | Lifecycle | Read back? | Notifies |
|---|---|---|---|---|
| Reject note | `<!-- board-pilot:reject round=N -->` | **discrete** — one per round, immutable | **yes**, round-scoped | yes, once per round |
| Run record | `<!-- board-pilot:run item=<id> -->` | **sticky** — one per run, edited in place | **never** | **once**, at the first post |

An author filter cannot separate these two: **both are the engine**. The only
thing that distinguishes them is the marker, which is why the marker is grammar,
not decoration.

**Notification asymmetry is the whole cost model.** An in-place edit never
re-notifies (the notification `reason` enum has no value for "edit"); a new
comment mails every subscriber. So the record table may grow as long as it likes
— volume inside the sticky is free — while a second comment is a real expense
paid by real people. That asymmetry, not size, is the budget.

### 1a. The narration blocks — the sticky SHOWS what each step produced

The table says *that* a stage passed; the **narration** shows *what it produced*. After
the summary, the run record carries one collapsible `<details>` per stage whose evidence
exists — the `spec` plan (`spec/plan.md`), the engine-tee'd `verify` output
(`verify/stdout`), the `review` verdict + reasoning (`review/verdict.json`) — so a reader
sees the plan and the review reasoning **on the issue**, without opening the PR or the
evidence dir. This is the asymmetry above put to work: narration is body growth inside
the ONE sticky, so it costs **no** extra notification.

Three properties keep it honest and safe; each is pinned by a test:

- **Read fresh from evidence every render, never parsed back from the body.**
  `_existing_rows` recovers only the table rows; the `<details>` are rebuilt from the
  evidence files on every tick, so they stay correct across ticks and re-armed runs and
  can never double. (This is why the Recorder needs the evidence dir — one template,
  `{item_id}` resolved per item, the same paths the runner tee'd into.)
- **Scrubbed, and inside a code fence.** Each block is `scrub`-redacted
  (redact-never-block, like every cell) but — unlike a cell — keeps its newlines,
  because it renders inside a fenced code block whose fence always out-lengths any
  backtick run in the content. So a quoted `board-pilot:reject round=N` cannot sit at
  byte 0 and can never be read back as a reject note: the §3 guarantee extends to the
  narration verbatim.
- **Capped under `max_body_chars`.** The skeleton is rendered once to measure it, the
  remainder (minus a margin) is the narration budget, and each block is additionally
  capped — truncated with a pointer to the PR dossier / evidence for the full text. Over
  GitHub's 65536-char ceiling the edit is rejected and `emit_guarded` swallows it into
  the ledger, so the whole record would silently vanish; the cap is not optional.

> **Honest bound — a bigger public surface.** Narration posts more agent prose (the
> plan, the verify output) onto a world-readable repo than the terse table did.
> Redaction runs on every block, but it is the §8 denylist — it catches token *shapes*
> and home paths, NOT a shapeless secret (an internal hostname, a customer name, a
> pattern-free password). A live e2e run confirmed the mechanism (fence, lift, byte-0,
> cap) but ran on benign content, so the redaction path was never exercised on a real
> secret. Treat the narration as public output: before pointing this pipeline at
> untrusted or secret-adjacent work, weigh tightening redaction or narrowing what the
> narration surfaces.

The honesty labels ride in each `<summary>` — `[machine-executed, agent-authored]` for
`verify`, `[agent] … not a measurement` for the plan and the verdict — the same
machine/agent split the table legend carries (§6). Only files the **engine** writes are
sources; never a stage teeing its own stdout (the `| tee` ban, §5). The `implement`
stage's own account is deliberately **not** a source yet — capturing it needs an
engine-owned narration sink, a separate change — and its *what* already shows as the diff
numstat in the dossier.

## 2. Why the reject note must NEVER be folded into the record

Tempting: one comment, everything in it. It breaks two things at once, silently.

- **The round IS the durable counter.** The read-back demands `round == item.bounces`
  exactly. A sticky carrying several round markers freezes the round at the
  **first** match forever, because a non-anchored find keeps the first (or last)
  hit rather than the current round's note.
- **The failure is silent degradation, not a crash.** The producer reworks blind
  through every `max_rounds` and parks, while the ledger reports a healthy loop
  the entire time. Nothing goes red. You would only find it by reading the note
  the agent actually received.

The reject note is immutable and round-scoped *because* it is the one artefact
whose identity a machine depends on. The record is editable *because* nothing
reads it back.

## 3. The byte-0 anchor — load-bearing, with the probe

`parse_reject` is anchored at byte 0:

```python
_REJECT_RE = re.compile(r"\A<!-- board-pilot:reject round=(\d+) -->\r?\n")  # .match, byte 0
note = text[m.end():]                                                       # offset, never a tail-split
```

**If you are about to "improve" this parser, run this first.** A run record
legitimately *quotes* a round's reject note. Under an unanchored `.search()`,
that record parses as a real reject note and is handed to the autonomous code
writer as its own feedback — the record steers the producer with its own
markdown:

```
anchored parse_reject(record) = (None, '')
unanchored .search()          = (1, '> add a negative-path test / ')

real note (CRLF)              = (1, 'add a negative-path test')
```

That is a live run against the shipped parser, not a thought experiment. The
hijack is **order-dependent and silent**: comments come back chronologically, so
whether the decoy or the real note wins depends on posting order.

Why the anchor beats it *structurally* rather than by escaping: a markdown quote
**always** carries a `> ` prefix, so a quoted marker can never sit at byte 0.
The record may therefore carry any body whatsoever, verbatim, and can never be
read back as a reject note.

| Body | unanchored | anchored (shipped) |
|---|---|---|
| real reject comment, n=1..3 | `(n, "note")` | **unchanged** ✓ |
| empty note | `(2, "")` | **unchanged** ✓ |
| **CRLF** (the API returns `\r\n`) | `(1, "add test")` | **unchanged** ✓ — `\r?\n`, or a silent blind-rework regress |
| record quoting a reject note | `(1, "## record…")` ← **hijack** | `(None, "")` ✓ |
| quoted marker (`> ` prefix) | `(1, "pwn")` | `(None, "")` ✓ |
| prose mentioning `board-pilot:reject round=9` | `(9, "…")` | `(None, "")` ✓ |

**The marker line carries only fixed machine tokens** (`kind`, `round`). No
handler string, no free text, no `[^>]*` extras: a `>` ends the tag early and a
`--` cannot appear inside an HTML comment at all — and **both fail silently**,
in both directions (visible junk in the body *and* a missed byte-0 find, so every
event posts a fresh comment and mails every subscriber). Attribution belongs in
the visible body, where it is prose instead of grammar.

The marker is validated at wiring time by running the **real parser** over the
**real rendered line**, not by a lookalike check that could drift from it. This
is not theoretical: a probe proved the hazard is reachable **from config alone** —
a `sticky_marker` that closes its own comment and opens a decoy renders a valid
round-1 reject note.

## 4. The placeholder rule

A record template is a `str.format_map` format string. **No Jinja, no eval, no
loops** — it describes a shape, it is not a program. `{rows}` arrives pre-joined
precisely because "no loops" means the *engine* iterates, never the template.

Two failure classes, deliberately split:

- **An unknown placeholder renders literally and can never raise.** A typo must
  cost the run nothing. This holds for *every* field expression — `{x}`, `{x!r}`,
  `{x:>10}`, `{x:d}`, `{x.y}`, `{x[0]}` — because the lookup returns a sentinel
  that survives all of them (a plain `str` would still raise on several).
- **A template that can never render at all fails LOUD** — an unclosed brace, a
  positional field (`{}` / `{0}`, and there are no positional args), or a format
  spec no string can satisfy (`{took:.2f}`). It raises at construction, i.e. at
  poller start, not on the tick.

The guarantee in the first bullet is real rather than hopeful **only** because
the load-time probe renders with string samples and every substituted value is a
`str` on every path: a template that renders at load cannot raise at render.

> **Posture note, so it is a decision and not a surprise:** a malformed template
> stops the poller at startup instead of silently falling back to the default. A
> silent fallback would leave an edit looking applied when it never was — the
> same class of lie as a dead knob. If you want soft fallback instead, that is a
> deliberate change, not a bug fix.

## 5. The evidence contract — the ENGINE writes evidence, never the stage

> **`require_evidence` without this contract is an existence check sold as a
> verification gate.**

A stage that *types* `5 passed in 0.15s` into its own evidence file passes an
existence check **identically** to one that actually ran the suite. The fix is
structural, not a matter of trust:

- The **parent** tees full `stdout` + `stderr` + the exit code into
  `<evidence_dir>/<stage_id>/`. `_spawn` already holds all three.
- The evaluated agent never touches the file.
- `evidence.require_deterministic: true` refuses at config load to let any stage
  whose `run:` is not `cmd:` be an evidence source.

So the output is **read from the pipe by the engine** and the exit code **belongs
to the engine**. Neither is forgeable by the stage's text.

**The honest limit:** `cmd:` is *not* a proof of determinism. It only proves the
engine, not a model, owned the pipe. A `cmd:` script is free to invoke a model
internally — `require_deterministic` cannot see that.

**What the engine still discards:** on success `stderr` is dropped, on failure
`stdout` is dropped. That is exactly why real test output needs the evidence
sink and not `result.notes`.

## 6. What the record refuses to say

Three fields are **absent rather than guessed**, and a test pins the absence:

- **The model.** Nothing pins `--model`; the runner spawns `["claude", "-p", prompt]`
  and the child resolves the CLI default, so the parent never learns it. Naming
  one would be a guess. *To make it real:* pin `--model X` in the stage and echo
  it into the evidence file — then the engine reports what it observed.
- **Tokens / cost.** Hardcoded to `0` on both real paths. Printing `tokens: 0`
  reads as "this run was free" and is a verify-before-claim violation.
- **TDD order.** Green at the end is not red-before-green, and it is not
  reconstructible afterwards. (The only honest proof would be a `verify-red`
  stage whose engine-captured **non-zero** exit is the evidence.)

`StageResult.tokens` is one attribute access away inside every hook. Printing it
is the easy, wrong thing — which is why its absence is a test, not a convention.

The `handler` column is cut for the same family of reason: it is config echo
(constant per stage, readable from the YAML) — and it would put an absolute local
path into a public comment. What varies, and what you actually tune, is
`criteria`.

## 7. The PR dossier — REAL vs CLAIM

The draft PR is the file a human merges on. Every section is labelled with what
it actually is:

| Section | Source | REAL or CLAIM |
|---|---|---|
| What was asked | issue title + body | **REAL** |
| What changed | `git diff --numstat` **+** the PR API's `additions`/`deletions`/`changedFiles` | **REAL, double-sourced** — local git AND the forge, independently |
| What was verified | engine-teed stdout of a `cmd:` stage + its exit code | **REAL, but labelled `[machine-executed, agent-authored]`** |
| What the reviewer said | the verdict sidecar's annotation | **CLAIM, loudly labelled** |
| Rework rounds | the durable bounce field + the round-scoped notes | **REAL** for the count and the notes. Per-round **deltas** are not — not reconstructible afterwards |
| Checks | a **link**, never a quoted result | **REAL, third party** |
| Where to look first | computed from the diff | mechanical, not agent-framed |

**The riskiest line is "tests passed".** Even engine-teed: the `implement` stage
wrote **the code AND the tests**. The suite passing confirms the agent **agrees
with itself**. Hence the label and one plain sentence in the body: *"the suite
ran for real and the exit code is the engine's; the assertions were written by
the same agent in the same run — green means self-consistent, not correct."* The
only **independent** evidence in the body is the diff numbers and CI, and the
body says so.

**Structural constraint: the body cannot cite its own CI.** CI starts *after* the
PR opens, and the `pr` stage is last and carries `gate: human`, so the engine
stops. The body links the checks tab and says that the gate belongs to the forge,
not to board-pilot. A post-gate stage that edited CI results in later would need
a shape beyond the human gate — inventing one would weaken the gate that is the
entire point.

**A green check with no artefact is not evidence.** An external review action
that reports success while producing no comment, no review and no timeline event
is exactly the failure this layer exists against — do not cite it.

**Never post `result.notes`.** It carries the repr of a CLI error, which embeds
**argv + stderr** — republishing it onto a public repo could republish a token
with it. It stays in the local ledger.

## 8. Redaction — an engine-owned scanner, two different postures

One definition (`engine/scan.py`), lifted out of the shipped bash so there is not
a copy per script. Two rules: a secret-shape denylist and a `repo-root` rule that
catches absolute home paths. It redacts **spans**, never blanking the whole body
— a body-stub on a single false positive destroys the note while the bounce
counter still climbs.

| Path | Posture | Enforced by |
|---|---|---|
| Record / reject note | **redact, never block** — a scan must not be able to stall the latch | the **engine** (`record.py` calls `scrub`) |
| PR body | **fail-closed** — a hit means exit 1, no PR, park | the **shipped `pr` stage**, calling the engine's scanner CLI |

> **Be precise about that second row.** The scanner is engine-owned; the *PR-body
> invocation* lives in the shipped `pr.sh`. Point the `pr` stage's `run:` at
> something else and **nothing scans the body** — no engine code does it for you.
>
> **And the honest ceiling on both rows:** this is a **regex denylist, not
> gitleaks**. It catches token shapes it already knows. It is better than the
> nothing that scans prose today, and it is not a secret scanner. Do not sell it
> as one.
