---
scope: core
description: Mandatory skill routing — when specific skills MUST be loaded before working
---
# Skill Routing Rules

## Purpose

Some skills are coordinators that enforce structured workflows, governance
rules, and learning loops. Bypassing them leads to ad-hoc work that
misses billing fields, skips preview gates, violates hard rules, and
loses institutional knowledge. These rules ensure the right skill is
loaded before work begins.

open-bridge itself ships no coordinator skill — this file defines the
**pattern**, not a concrete instance. An org overlay adds its own entries
under `rules/org/skill-routing.md`, one per coordinator, in the shape below.

## Pattern: keyword triggers → mandatory skill → rationale

Each entry names a coordinator skill and the signals that mean it must
load before any analysis, issue creation, email, or log entry:

- **When:** the triggering signals — stakeholder names, system/product
  names, artifact types (invoice number, correlation ID, ticket ID),
  project/board names, infrastructure identifiers, topic keywords.
- **Action:** load `<coordinator-skill>` before doing anything else.
- **Why:** state what the coordinator enforces — this is what justifies
  routing over ad-hoc work. A well-formed coordinator typically covers
  some subset of:
  1. Structured writes (issues via a project-manager skill, never a raw CLI call)
  2. Field conventions on the project/board it targets
  3. Preview-before-execute for stakeholder-facing actions
  4. Immediate logging with correct context tags
  5. A learning loop — new failure patterns get proposed back
  6. Classification fields required for billing or governance
- **Anti-pattern:** "It's just a quick analysis" or "let me just check
  the logs" — these are exactly the cases where a coordinator adds the
  most value, because quick, informal work is the most likely to skip
  documentation and billing.
