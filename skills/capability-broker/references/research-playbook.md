# Research Playbook — bounded option-finding

When a route needs research (`[r]`, or to decide the provision-vs-skill tree),
run a **bounded** pass. Prefer delegating to the `deep-research` skill or a
single sub-agent with WebSearch/WebFetch so raw search output stays out of the
main context — return only the distilled options.

## Output contract

```
Capability: <one line>

Options (2–4, each sourced):
  1. <name> — <CLI | MCP | API/SaaS | library>
     install/auth: <how>          license: <SPDX>   cost: <free/paid + verified?>
     maintenance: <last release / activity>   fit: <why it matches>   source: <url>
  ...

Recommendation: <option N> — <1–2 line rationale>
Verdict: provision-tool | scaffold-skill | both | one-off-only
```

## Bounds

- Cap searches/time (`research.max_sources`, default 4). Stop at "good enough to
  decide", not "exhaustive".
- **Cite every source.** No option without a URL. No invented capabilities.
- Delegate the heavy fan-out; return the distilled table, not the transcript.

## Honesty rules (hard)

- **Never trust a "free" / "no-install" / "no-signup" claim from a search
  summary.** Default to "paid / needs verification" until proven from the
  primary source. (Standing Bridge lesson: pay-to-list and paywalled offerings
  routinely read as "free" in search snippets.) `research.default_paid: true`.
- **Verify install/auth from the primary source** (the tool's own docs/repo),
  not a third-party blog.
- **Flag lock-in and outward data flow.** If an option sends data to a third
  party, say so explicitly — it changes the gate (provision becomes an outward
  step).

## Ranking (all else equal)

1. **Honor the instance's standing constraints** — e.g. a data-egress-region
   rule, a no-third-party-CDN rule, an EU-only rule. CORE only says *honor them*;
   the rules themselves are USER/ORG overlays. Read them before ranking.
2. Open / self-hostable / low lock-in over proprietary SaaS.
3. Already-present ecosystem fit (a CLI the machine can `brew install` beats a
   new account to provision) — less new attack surface, fewer gates.
4. Maintenance health (recent activity) over abandoned-but-popular.

## Where research stops

Research returns options + a recommendation + a verdict. It does **not** install,
sign up, or mint anything — that's the provision step, behind per-action gates
(`provision-vs-skill.md`).
