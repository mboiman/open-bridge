---
summary: "The method behind spec-kickoff — Karpathy's spec→verifier→environment, verified sources, EARS / verification-ladder / gate cheat-sheets, and honest corrections to the popular video."
type: reference
last_updated: 2026-06-16
related:
  - ../SKILL.md
  - ../assets/SPEC.md
---

# The Method behind Spec-Kickoff

Why the skill is shaped the way it is, the **verified** sources behind it, the
cheat-sheets the phases reference, and the corrections that keep the framing honest.

The one-line thesis: **the skill is spec-kit's discipline + Kiro's EARS/gates, expressed
through Anthropic's own `interview → SPEC.md → verify-loop` primitives, wired into the
environment you already have.** Not a new framework.

---

## The three layers

Origin: a popular YouTube walkthrough (Austin Marchese, *"Stop Prompting Claude. Use
Karpathy's Method Instead."*) distilled Karpathy's way of working into **spec → verifier →
environment**. The framing is useful; the skill re-grounds each layer on primary sources
and official Claude Code guidance.

### Layer 1 — Spec  (you own the goal)

LLMs are brilliant where things are *measurable* and weak where the signal is contextual.
The bridge from your understanding to the model's compute is a **spec** — and it must be
*yours*. Karpathy:

> "You can outsource your thinking, but you can't outsource your understanding."
> — Sequoia **AI Ascent 2026** fireside, *From Vibe Coding to Agentic Engineering*
> (~30 Apr 2026). He self-cites it: https://x.com/karpathy/status/2049907410303865030

He is explicit that a high-level "plan mode" is not the design artifact:

> "I don't even fully like 'plan mode' as a concept, **though it is useful**." … "You work
> with your agent to design a detailed spec, maybe basically the docs." — same talk.

(Note the nuance: plan mode is *useful*, just not a substitute for a co-authored spec.)
Anthropic's own best-practices page operationalises exactly this:

> "I want to build [X]. Interview me in detail using the AskUserQuestion tool. Ask about
> technical implementation, UI/UX, edge cases, concerns, and tradeoffs… Keep interviewing
> until we've covered everything, then write a complete spec to SPEC.md." … "The most
> useful specs are self-contained: they name the files and interfaces involved, state what
> is out of scope, and **end with an end-to-end verification step**."
> — https://code.claude.com/docs/en/best-practices

**Three moves inside the spec** (from the video, sharpened):
1. **Goal, not task** — uncover the decision the output drives; interview to extract it.
2. **Agile slice** — thinnest end-to-end slice with a verifiable check; defer the rest.
3. **Precision + explicit decision-verification** — EARS requirements; flag assumptions
   and have the human confirm them.

### Layer 2 — Verifier  (make it checkable)

Why a verifier *specifically*: the model is a **ghost**, not an animal.

> "We're not building animals, we're summoning ghosts." — Karpathy, **Dwarkesh Podcast**,
> 17 Oct 2025. Ghosts = fully digital artifacts trained by *imitation* of human text; no
> continual learning; each conversation materialises, helps, and vanishes.

Because the output is plausible-*shaped* by imitation, it must be checked against a real
verifier — not trusted because it sounds right. This dovetails with his **verifiability /
"jagged intelligence"** thesis: LLMs spike to superhuman where outputs are *verifiable*
(code, math) and stagnate where verification is hard. So: **build things whose success is
verifiable**, and if the goal isn't, surface that as a risk.

The single biggest quality lever, per the creator of Claude Code:

> "Give Claude a way to verify its own work. If Claude has that feedback loop, it will
> 2–3× the quality of the final results." — Boris Cherny, relayed by John Kim,
> *How the Creator of Claude Code Actually Uses Claude Code* (Push To Prod, ~Feb 2026).
> **Cite as practitioner-reported**, not an Anthropic benchmark — the *direction* is
> officially backed (the best-practices page has a whole "give Claude a way to verify"
> section); the *exact multiplier* is anecdotal.

### Layer 3 — Environment  (the workshop)

Spec + verifier need somewhere to live: context, reusable skills, and guardrails. Karpathy's
viral **knowledge-base / "idea file"** concept (tweet ~3 Apr 2026,
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) is the context layer —
a project-local folder the agent reads from:

- **`raw/`** — your curated, immutable sources (the LLM reads, never edits);
- **`wiki/`** — LLM-maintained, interlinked markdown (summaries, entity pages, synthesis);
- **`outputs/`** — generated responses/reports;
- plus a CLAUDE.md-style schema telling the agent how it's organised.

> Frame it as "build the context the agent stands on" — **not** "your data is your moat"
> (that slogan is a blog gloss, not Karpathy's wording).

On a built-out Claude Code setup (e.g. a Bridge), Layer 3 is **already there** — CLAUDE.md
+ rules + hooks + skills. The skill points at it; it only *adds* a per-build guardrail when
the build needs one.

---

## EARS cheat-sheet  (used in Phase 1c)

EARS = *Easy Approach to Requirements Syntax* (from Amazon Kiro's spec format,
https://kiro.dev/docs/specs). Constrained sentence patterns that force coverage of happy path,
edge cases, and failure modes — and each line maps cleanly to an acceptance check.

| Pattern | Shape | Use for |
|---|---|---|
| Ubiquitous | THE SYSTEM SHALL `<response>` | always-true invariants |
| Event-driven | WHEN `<trigger>` THE SYSTEM SHALL `<response>` | reactions to events |
| State-driven | WHILE `<state>` THE SYSTEM SHALL `<response>` | behaviour during a mode |
| Optional | WHERE `<feature included>` THE SYSTEM SHALL `<response>` | feature-flagged behaviour |
| Unwanted | IF `<condition>` THEN THE SYSTEM SHALL `<response>` | **edge cases & failure modes** |

Tip: the `IF…THEN` lines are where most teams under-specify. Write those first — they
become your most valuable test cases.

---

## Verification ladder  (used in Phase 2b)

Map **every** acceptance criterion to the *strongest available* check. Prefer higher rungs.

1. **Deterministic check** — unit/integration test, build exit code, linter, typechecker,
   diff against a known-good fixture. Cheapest reliable signal; always prefer it.
2. **External signal** — CI status per SHA (`gh pr checks`, `gh run watch`), a deploy probe
   that confirms the thing actually deployed, a reference doc / historical report used as
   ground truth, real data.
3. **Judge (LLM / subagent)** — *only* where there is no ground truth. Calibrate against the
   known pitfalls: use a **fresh context** (not the writer); hand it the **rubric /
   acceptance criteria**, not "is this good?"; tell it to flag **only** correctness /
   requirement gaps; prefer an *agent-as-judge* that runs checks over one-shot scoring.
   LLM-as-judge bias (position, verbosity, authority, self-enhancement) is **measured and
   real** — never trust a single judge pass.

> **Unverifiable → don't ship.** If a criterion can't reach even rung 3, mark it
> UNVERIFIABLE and decide deliberately: make it verifiable, or cut it. (Anthropic's
> "trust-then-verify gap": *"If you can't verify it, don't ship it."*)

---

## Gate-strength menu  (used in Phase 2c)

From Anthropic's "give Claude a way to verify its work" — four escalating strengths.
Pick per criterion by cost-of-being-wrong.

| Strength | Mechanism | When |
|---|---|---|
| **One-prompt** | run the check and iterate in the same message | cheap, low stakes |
| **Goal condition** | set it as a `/goal` condition; an evaluator re-checks every turn until it holds | multi-step work in one session |
| **Stop-hook** | a Stop hook runs the check as a script and **blocks the turn from ending** until it passes (Claude can override after 8 consecutive blocks) | must-not-forget invariants |
| **Second opinion** | a **fresh subagent tries to refute the result** — the worker isn't the grader; show evidence (test output, command+return, screenshot), don't assert | high reversal cost |

Maps onto the always / ask / never buckets via Claude Code's real mechanisms:
`/permissions` allowlist (always-do) · auto-mode / default prompt (ask-first) · PreToolUse
hook `exit 2` (never-do).

---

## Second-model critic  (used in Phase 2d)

**Default — native, no dependency.** A fresh Claude subagent reviews the diff against the
spec's acceptance criteria in a clean context; the bundled `/code-review` skill does this.
Tell it to flag *only* correctness / requirement gaps (a reviewer asked to find gaps will
invent some — guard against over-engineering).

**Opt-in upgrade — cross-model (OpenAI Codex plugin).** A different training lineage is
harder to fool with shared sycophancy bias. First-party plugin `openai/codex-plugin-cc`:

```
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

Adds `/codex:review` (read-only), `/codex:adversarial-review` (steerable challenge),
`/codex:rescue` (hand off stuck code). Treat it as a *luxury*, not a dependency — a second
opinion is nice, the day's work isn't. (The user's stack is Claude-Code-only; lead native.)

---

## Protected-paths guardrail  (used in Phase 3)

The concrete "never-do" enforcement: a **PreToolUse hook that exits 2** to hard-block edits
to sensitive paths (unlike CLAUDE.md text, which is advisory). In `.claude/settings.json`:

```json
{ "hooks": { "PreToolUse": [
  { "matcher": "Edit|Write",
    "hooks": [ { "type": "command",
      "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/protect-files.sh" } ] } ] } }
```

`protect-files.sh` reads the tool input (JSON on stdin, incl. file path); if the path
matches a protected glob (`.env`, `*.lock`, `.git/`, an "Important / Don't Edit" dir, prod
config, client data) it does `echo "blocked: …" >&2; exit 2` — Claude gets the stderr as
feedback and proposes a safe alternative. (https://code.claude.com/docs/en/hooks-guide) You can
ask Claude to write the hook: *"Write a hook that blocks writes to `<dir>`."*

---

## Corrections to the popular video  (keep the framing honest)

The skill deliberately does **not** repeat these errors:

1. **Venue.** The video's "AISN 2026" is a mis-transcription of **Sequoia AI Ascent 2026**.
   The *ghosts* quote is from the **Dwarkesh Podcast (Oct 2025)**; the *outsource thinking*
   and *plan-mode* quotes are from **AI Ascent 2026 (Apr 2026)** — don't conflate venues.
2. **The "car wash, 50 m, drive or walk?" example is NOT Karpathy's.** It's a community
   benchmark (the "Car Wash Test") illustrating his verifiability thesis. The *thesis* is
   his; the *example* is the creator's/community's.
3. **Plan mode isn't "bad."** Karpathy called it *"useful,"* just not a substitute for a
   co-authored detailed spec. Don't flatten it into rejection.
4. **"2–3× quality"** is Cherny-via-Kim (Push To Prod), not a cited Anthropic stat. Cite
   the direction as official, the multiplier as anecdotal.
5. **"Your data is your moat"** is a secondary-blog gloss. The real, attributable concept is
   the `raw / wiki / outputs` knowledge base + "idea file."
6. **"English is the hottest new programming language"** predates the Software 3.0 talk
   (Karpathy tweet, Jan 2023). And **"vibe coding"** (coined Feb 2025) is the *floor-raising*
   mode he has since moved *past* toward **agentic engineering** — this skill anchors on the
   latter, not on vibe coding.

---

## Sources

- Anthropic — *Claude Code best practices*: https://code.claude.com/docs/en/best-practices
  (interview→SPEC.md, Explore→Plan→Code→Commit, "give Claude a way to verify", adversarial
  review, trust-then-verify gap). Hooks: https://code.claude.com/docs/en/hooks-guide
- Karpathy — Dwarkesh Podcast, 17 Oct 2025 ("summoning ghosts",
  https://www.youtube.com/watch?v=lXUZvyajciY); Sequoia AI Ascent 2026, ~30 Apr 2026
  ("outsource thinking/understanding", plan-mode-vs-spec, verifiability), summary
  https://karpathy.bearblog.dev/sequoia-ascent-2026/ + self-cite
  https://x.com/karpathy/status/2049907410303865030; YC AI Startup School, 17 Jun 2025
  (Software 3.0, autonomy slider, keep-on-a-leash); knowledge-base / idea-file tweet ~3 Apr
  2026 (https://x.com/karpathy/status/2040470801506541998) + LLM-Wiki gist
  https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- GitHub **spec-kit** — https://github.com/github/spec-kit (`spec-driven.md`):
  Constitution→Specify→Clarify→Plan→Analyze→Tasks→Implement; "code serves specifications."
- Amazon **Kiro** — https://kiro.dev/docs/specs : requirements/design/tasks; **EARS** format.
- Sean Grove (OpenAI), *The New Code* — "specs are the new source code" (framing only).
- Boris Cherny — via John Kim, *Push To Prod* (~Feb 2026,
  https://getpushtoprod.substack.com/p/how-the-creator-of-claude-code-actually):
  feedback-loop = #1 lever, ~2–3×; "PR anti-pattern → update CLAUDE.md" loop.
- OpenAI **Codex plugin for Claude Code** — https://github.com/openai/codex-plugin-cc
- LLM-as-judge bias — *Bias in the Loop* (https://arxiv.org/html/2604.16790v1);
  *Agent-as-a-Judge* (https://arxiv.org/pdf/2508.02994).

> Full research with per-claim confidence levels (USER-local, not shipped with this skill):
> the originating instance keeps it under the kickoff task's `research/` folder.
