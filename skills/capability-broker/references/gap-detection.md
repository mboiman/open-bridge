# Gap Detection — true gap vs false gap

The guard that runs before any offer. Its job is to **not** offer when the
capability already exists in some form. A false offer (proposing to build a skill
that already exists) is the #1 failure mode, so the sweep errs toward "something
covers this → don't offer."

## Taxonomy

| Tag | Condition | Action |
|---|---|---|
| **T1 true gap** | no skill, no wired tool, not native | OFFER |
| **T2 dormant skill** | a skill plausibly covers it but didn't fire | load it; if mis-trigger, log a trigger-correction. STOP. |
| **T3 native** | Claude can do it with built-in tools, no external piece | just do it |
| **T4 one-off** | real gap, genuinely single-use | do it ad-hoc + ledger note |
| **T5 declined/policy** | declined this session, or must-not-do | silent / refuse |

## Procedure (cheap, in the orchestrator)

Run in order; first match wins.

1. **Skill sweep (most important).**
   ```bash
   grep -ril "<keyword1>\|<keyword2>" skills/*/SKILL.md
   ```
   Sweep `name` + `description` (trigger phrases) for the capability's keywords.
   A plausible hit → **T2**. Load that skill. If it should have triggered on the
   user's phrasing but didn't, that's a trigger-correction, not a capability gap
   — append to `work/_learning/trigger-corrections.md` (existing loop) and stop.

2. **Native check.** Can Claude do this with tools it already has (read/write
   files, search the repo, summarize, transform text, run a one-liner)? No
   external binary, API, or service needed → **T3**. Do it. No gap.

3. **Tool sweep.** Is the capability already wired?
   - CLI on `PATH`: `command -v <tool>`
   - MCP server: present in `.mcp.json` (if the repo has one)
   - Account/service: a file under `identity/accounts/`
   A hit → not a gap (use what's wired, possibly via a thin one-off).

4. **Cooldown check.** Has the user declined this gap this session, or does
   `work/_learning/capability-gaps.md` mark this gap `declined`/`silenced`
   within the cooldown window (`decline_cooldown_sessions`)? → **T5**, stay silent.

5. **Persist-worthiness.** Real gap, but is it single-use? If the user signals
   "just this once" or it's plainly a throwaway → **T4** (one-off, ledger note).
   Otherwise → **T1**, surface the offer.

## Keyword extraction

Pull keywords from the *capability*, not the surface phrasing:
- "can you turn these HEIC photos into JPG" → `heic`, `image`, `convert`
- "pull my open Linear issues" → `linear`, `issue`, `tracker`
- "transcribe this call" → `transcribe`, `audio`, `meeting`

Broaden synonyms before deciding T1 — a narrow sweep misses a covering skill.

## Sub-agent behavior

A sub-agent that hits a gap mid-task does **not** offer. It reports the gap in
its summary ("capability gap: X — no skill/tool found") and lets the orchestrator
run the offer with the user. Only the orchestrator interacts with the user.

## Edge cases

- **Adjacent skill, wrong shape** — a skill is close but genuinely can't do the
  asked thing (e.g. a PDF skill, but the user needs OCR). That's a **real gap**
  (T1) for the missing capability, even though a neighbor exists. Note the
  neighbor in the proposal so the scaffold/extend decision is informed (extend
  the neighbor vs new skill is a `skill-creator` call).
- **Capability is policy-blocked** — destructive, mass-outreach, detection-evasion
  → T5, refuse per the Bridge's safety posture; do not offer to build it.
