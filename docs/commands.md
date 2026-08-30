---
summary: The slash commands each shipped skill registers, and what invokes them.
type: reference
last_updated: 2026-08-29
related:
  - AGENTS.md
  - docs/skill-distribution-architecture.md
---

# Commands

In Claude Code each skill registers its own slash-command trigger through its
`description:` frontmatter. There are no separate files in `.claude/commands/`,
and invoking a skill via the Skill tool is equivalent to typing its command.
Other tools (Codex, Copilot, Gemini, Cursor) load the skill from `skills/`
directly.

This table is the CORE set. An instance carries more, and which ones is
per-instance, so the live list is the skill listing itself rather than anything
written down here.

| Command | Backing skill | Action |
|---------|---------------|--------|
| `/bridge-status` | `bridge-status` | Status dashboard: ecosystem, agents, work, remotes |
| `/bridge-explorer` | `bridge-explorer` | Ecosystem + repo-layout + constellation visualizations |
| `/briefing` | `briefing` | Daily briefing: board, git activity, goals, alerts |
| `/archive` | `archive` | Archive week + create summary |
| `/debrief` | `debrief` | Process transcripts: 7-category insights, tasks, protocols (full / `--quick` / `--all` / `--date`) |
| `/bridge-onboard` | `bridge-onboard` | New user setup or reconfiguration |
| `/channel` | `channel` | Channel management: list, health, deploy, start/stop |
| `/remote` | `remote` | Remote management: status, health, logs, restart, sync |
| `/workload` | `workload` | Declared runs: `declare`, `validate`, `render`, `provision`, `list`, `show`, `reconcile`, `view`, `publish`, `adopt`, `retire` |
| `/schedule` | `schedule` | Scheduled tasks: list, create, deploy, disable |
| `/promote` | `bridge-promote` | Promote CORE changes upstream (scope:core → `bks-lab/open-bridge`, scope:org → your optional org overlay) |
| `/overlay` | `bridge-overlay` | Subscribe to org overlays + materialize scope:org content into the live tree (downstream inverse of `/promote`) |
| `/contribute` | `bridge-contribute` | Scan user branch for upstream-worthy contributions |
| `/calendar` | `calendar` | Calendar entries: list, add, cancel, confirm, show, status |
| `/mandants` | `mandants` | Mandant management: list, add, show, add-person |

---
