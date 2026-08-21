---
summary: "scripts/okf-export.py — exports a Bridge instance's knowledge surfaces (work/, docs/, rules/, examples/) as a static Open Knowledge Format (OKF) v0.2 bundle, with a scope flag that gates what a public export may contain."
type: guide
last_updated: 2026-08-21
related:
  - ../scripts/okf-export.py
  - ../scripts/extract-frontmatter.py
  - ../scripts/gen-board.py
  - extension-model.md
  - memory.md
---

# OKF Export

`scripts/okf-export.py` walks a Bridge instance's knowledge surfaces and emits
a static [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
(OKF) v0.2 bundle — one markdown file per *concept*, a per-type `index.md`,
and a root `index.md` declaring the bundle's `okf_version`. It is read-only
against the source repo: nothing under `work/`, `docs/`, `rules/`, or
`examples/` is ever rewritten. The bundle is a **derived artifact**, disposable
and regenerable on every run.

## Background

[Open Knowledge Format (OKF)](https://okf.md/) is an open spec Google Cloud
announced in June 2026, formalizing the same markdown + YAML frontmatter
pattern that already underpins this repo's `docs/`, `rules/`, and memory
files: one file per concept, the file's path doubles as the concept ID, and a
single required `type` field says what kind of concept it is. It was created
by Sam McVeety and Amir Hormati at Google Cloud and is published under
Apache-2.0 in `GoogleCloudPlatform/knowledge-catalog`; the spec itself names
no central authority, and the licence is what keeps it forkable. The W3C
Holon Community Group (launched 2026-06-19) is a **separate** effort whose
DataBook profile layers optional formal semantics on top of OKF bundles; it
does not govern OKF, and plain OKF bundles stay valid without it.

open-bridge adopts OKF as an **interop target for the knowledge layer only**:
`work/`, `docs/`, `rules/`, `examples/`, and memory facts map cleanly onto OKF
concepts. The behaviour layer — skills, standing orders, task lifecycle — is
deliberately left un-OKF-shaped; it is process the agent executes, not a
knowledge concept to export.

Sources: https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing · https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf · https://okf.md/

## Why an exporter and not in-place conformance

A Bridge instance already carries most of an OKF concept's shape —
frontmatter (`title`/`summary`/`last_updated`), a markdown body, and
`[[wikilink]]`-style cross-references in Memory and docs. Rather than
rewriting hundreds of source files to a stricter shared schema, the exporter
maps what already exists onto OKF at export time: **tolerant-consume**
(loose, hand-rolled parsing of whatever frontmatter shape a file already has)
feeding a **strict-produce** bundle (every concept file carries the full OKF
frontmatter contract). The mapping logic is additive and reversible — delete
`scripts/okf-export.py` and the output directory, and the source tree is
unaffected.

## Concept mapping

| Source | OKF `type` | Notes |
|---|---|---|
| `work/tasks/<slug>/STATUS.md` | `task` | slug = the task's directory name |
| `work/streams/<slug>/STATUS.md` | `stream` | long-running, never `done` |
| `work/done/<month>/<slug>/STATUS.md` | `task` | closed tasks still map to `task` |
| `work/**/deliverables/*.md` | `deliverable` | any depth under `work/` |
| `docs/**/*.md` | `doc` | |
| `rules/**/*.md` | `rule` | |
| `examples/**/*.md` | `example` | |
| `<memory-dir>/*.md` fact files | `memory` | user scope only; see [Memory](#memory) |

For every source file:

- **`title`** — frontmatter `title` → the first `# ` H1 line in the body →
  the concept's slug (as a last-resort fallback).
- **`description`** — frontmatter `description` → `summary` → `headline`
  → empty string. Never derived from the body — a missing field means an
  honestly empty description, not a guessed one.
- **`generated`** — `{ by, at }`. See [Provenance and trust](#provenance-and-trust)
  below; `by` is always written, `at` only when the source date could be
  proven.
- **`bridge_status`** — the source's Bridge workflow state (`doing`,
  `review`, …), under a namespaced key. **Never** written to OKF's own
  `status`: see [the status homograph](#the-status-homograph).
- **`tags`** — `[status, context]` frontmatter values, only the ones present.
- **`resource`** — the concept's source: its repo-relative path
  (`docs/foo.md`), or `memory/<filename>` for memory facts. Points a
  consumer back at the underlying asset.

Empty optional fields are **omitted**, never written as `""` or `[]` — OKF
states that absence carries meaning, so an empty value is a different (and
false) claim from "not recorded".

## Provenance and trust

`generated.by` names **the transformation that produced the bundle document**,
not the author of the underlying knowledge. It defaults to
`okf-export/<EXPORTER_VERSION>`; `--generated-by` overrides it with any OKF
actor (`<producer>/<version>`, `human:<id>`, `process:<id>`), and an invalid
value is rejected with exit code 1 rather than written — the value is written
verbatim into every concept, so an unconstrained string would be an unreadable
provenance claim, and one containing a newline or quote would be malformed
YAML. (Trust *tiers* derive from `verified`, which this exporter never emits,
so a wrong actor here misattributes provenance but cannot inflate a tier.)
The actor is **never** derived from `git config`, `git log` or `$USER`: a
committer is not a knowledge author, it would inject a real identity into a
`--scope core` bundle, and it would make the output depend on clone state.

`generated.at` carries the source's `last_updated` → `created`, normalized to
an ISO instant with an explicit UTC offset, as OKF requires. Bridge sources
hold bare dates, so `2026-07-02` is widened to `2026-07-02T00:00:00Z`.
**This is a documented tradeoff:** midnight is a time of day the source never
stated, but the invented component is bounded under 24h, the date is fully
recoverable, and midnight is the *earliest* instant consistent with the stated
date, so no consumer ever reads the content as fresher than the evidence
supports. A date that cannot be proven is **omitted rather than guessed** — a
partial `2026-03`, the literal `YYYY-MM-DD` that `work/templates/STATUS.md`
seeds into every new task, a calendar-impossible date, or free text. The
manifest reports how many concepts ended up without one.

### What this exporter deliberately does not emit

Each of these drives consumer *behaviour*, not just display, so a fabricated
value does not read as noise — it reads as a false claim.

| Field | Why not | What would change that |
|---|---|---|
| `verified` | Nothing in a Bridge instance is a verification event. `status: review` means a review is *pending*; `status: done` means the work finished, not that anyone confirmed the document against its sources. Trust tiers are derived purely from this field, so a synthetic entry silently promotes every concept out of `unverified`, and a `human:` actor would be an outright false claim of human sign-off. | A source file carrying a real `verified` stamp written by something that actually checked it. The exporter would pass it through; it will never synthesize one. |
| `sources` | `related:` is *see-also*, not *derived-from*, and its paths are inconsistently based, so many entries would not resolve at all — while `resource` is required per entry. A consumer may recurse into `sources` and propagate credibility, so feeding it see-also links manufactures lineage edges rather than adding inert noise. The concept's own origin is already fully expressed by `resource`. | An explicit derivation field in the source (e.g. `derived_from:`) with a resolvable target. |
| `stale_after` | No Bridge field says when a *document* stops being true. A deadline says when *work* is due, and a derived `last_updated + N days` would invent a retention policy the Bridge has never declared. It is a consumer **gate**, so a fabricated horizon suppresses content that is perfectly current. | A source carrying a real expiry instant. Note the type: an **absolute ISO instant with an offset** (`2026-09-23T00:00:00Z`), never a bare `YYYY-MM-DD` and never a TTL like `90d`. |
| `status` | See below. | — |
| Attested Computation | `runtime` is required for that type and the Bridge has no runtime, parameters, executor or attester. A consumer *executes* an attested computation, so a half-populated contract would turn a documentation bundle into something a consumer tries to run. | Nothing foreseeable. |

### The status homograph

OKF's `status` is **document readiness** (`draft` | `stable` | `deprecated`).
A Bridge `status` is **work state** (`backlog` | `doing` | `review` | `done`).
The two are orthogonal axes that happen to share a field name, and `draft` is
a homograph across them: a Bridge task in draft state is not a document that
is "not yet reviewed, possibly incomplete". A finished task's write-up is the
*most* current document in the bundle, not the least.

So no concept ever carries a top-level `status`, not even behind an
enum whitelist. Absent `status` already means `stable` in OKF, which is the
true claim about an exported Bridge write-up. The Bridge value travels under
`bridge_status` (and, unchanged from v0.1, in `tags`).

Frontmatter parsing is hand-rolled (no PyYAML dependency). It keeps
`scripts/extract-frontmatter.py`'s `# yaml-language-server: $schema=...`
comment-prolog skip and reads the same flat `key: value` scalars as
`scripts/gen-board.py`'s `parse_status()`, but it deliberately diverges from
that script on three points, because `gen-board.py` is lenient where YAML is
not:

- **Quoting is resolved before inline comments**, the order YAML itself uses.
  A `#` inside a quoted scalar is a literal character, so
  `headline: "... in review as PR #214"` keeps its issue number. On an
  *unquoted* scalar a ` #` still opens a comment and is still stripped, and
  `value#nospace` (no whitespace in front of the hash) stays whole.
- **A quote closes a quoted scalar only where a quote may close one**: with
  nothing behind it, or an inline comment. Anything else means that quote was
  a character inside the value, and the value then falls back to the plain
  path *whole* rather than being cut at it. `title: 'Michael's bridge'` yields
  `Michael's bridge`, and `title: "He said "stop" once"` yields
  `He said "stop" once`. PyYAML rejects both of those lines outright, so there
  is no conformant reading to defer to; keeping a malformed value whole is the
  lesser failure. Doubling the inner apostrophe (`'Michael''s bridge'`) or
  switching quote style still makes the line legal YAML, which is worth doing
  for any consumer that is not this exporter.
- **A `---` fence counts only at column 0**, for the opening and the closing
  fence alike. A block-scalar continuation line is indented by definition, so
  an indented `---` inside a `title: |` block is content, not the end of the
  frontmatter block.

All three rules are about where a value or a block **ends**, and none of them
raises. Where a source is malformed by YAML's own rules, content can still be
dropped silently and the source is what has to be fixed:

- An **unquoted** value whose last character is a quote loses that character:
  `title: monitor is 12"` yields `monitor is 12`, and a `description:` ending
  in a quoted phrase loses that phrase's closing quote. Wrap the whole value in
  the other quote style (`description: 'a phrase like "this"'`) to keep it.
- A frontmatter block closed by an **indented** `---` no longer ends there: the
  parser reads on to the next `---` at column 0, so the body text in between is
  consumed as frontmatter and never reaches the bundle. The keys still parse,
  so nothing marks the loss. Put the closing fence at column 0; an indented one
  used to close the block.

A file with no frontmatter block at all still exports cleanly: its title falls
back to the first H1, its description to `""`.

## Wikilink resolution

Kebab-case `[[slug]]` references (`[a-z][a-z0-9-]*` only — bash
`[[ -f … ]]` conditionals inside code blocks never match) are resolved
**at export time**, against an index built from the slugs of every concept
in the current export — never rewritten in the source repo:

- **Resolved** — `[[slug]]` becomes a bundle-root-relative markdown link:
  `[slug](/<type>/<slug>.md)` (the leading `/` is root-relative *within the
  bundle*, not the filesystem — concept files live inside `<type>/`
  subdirectories, so a plain `<type>/<slug>.md` link would resolve wrong from
  inside another `<type>/` directory). On a cross-type slug collision the
  `memory` concept wins the link target — wikilinks are memory references
  by convention.
- **Unresolved** — the `[[slug]]` text is left completely untouched (OKF
  tolerates dangling references; rewriting them would corrupt content); the
  slug is collected into the manifest's `unresolved_wikilinks` list so a
  maintainer can see what didn't resolve.

## Scope

`--scope` controls which sources are walked — this is the export's privacy
boundary, not a cosmetic filter:

- **`user`** (default) — everything: `work/` (tasks, streams, done,
  deliverables) plus `docs/`, `rules/`, `examples/`, and the instance's
  auto-memory facts. Intended for a private, full-instance export — the
  bundle will contain whatever PII the source tree contains, so treat the
  output directory exactly as sensitively as `work/`.
- **`core`** — `docs/**/*.md` and `examples/**/*.md` only. `work/`,
  `rules/`, and memory are excluded entirely. This is the shape a public
  demo export may take. **Run `scripts/no-scrub-leak.py` over the output
  directory before publishing a `core`-scope bundle** — the exporter itself
  does not scan for leaked content, it only restricts which source trees it
  reads.

## Memory

In `user` scope the exporter also walks the instance's **auto-memory**
directory — the harness's per-project store of durable facts, which lives
*outside* the repo at `~/.claude/projects/<encoded-root>/memory` (the
absolute repo path with `/` replaced by `-`). The default derivation can be
overridden with `--memory-dir`; a missing directory is skipped with a
notice, never an error (fresh instances legitimately have none).

A file qualifies as a memory fact when it carries frontmatter with a
`name:` key; that kebab-case name becomes the concept slug — which is
exactly what `[[wikilinks]]` reference, so memory links resolve naturally.
`MEMORY.md`, `MEMORY-ARCHIVE.md`, `PROVENANCE.md`, `_`-prefixed files, and
frontmatter-less strays are never exported.

## Output layout

```
<out>/
├── index.md              # okf_version (the only key OKF permits here)
├── task/
│   ├── index.md
│   └── <slug>.md
├── stream/
│   ├── index.md
│   └── <slug>.md
├── deliverable/…
├── doc/…
├── rule/…
└── example/…
```

Only type directories with at least one concept are created — a `core`-scope
export never creates `task/`, `stream/`, `deliverable/`, `rule/`, or
`memory/`. Each concept file carries an OKF frontmatter block (`type`,
`title`, `description`?, `resource`, `generated`, `bridge_status`?, `tags`?,
in that order, with the optional ones omitted when empty) followed by its
resolved body. `index.md` is a reserved
filename: the root `index.md` is the sole exception carrying frontmatter, and
it carries **`okf_version` alone** — the one key OKF permits in an index file
— with scope and concept count stated in the body prose instead. Every
per-type `index.md` carries no frontmatter at all, only a heading and a
directory listing in the spec's `* [Title](slug.md) - description` form.
`index` and
`log` are also reserved concept slugs. A source file that would otherwise map
to either, and any other same-type slug collision (e.g. two differently-pathed
`README.md` sources), is disambiguated with a numeric suffix: the **lowest one
not already claimed** by another concept of the same type. A concept keeps its
own natural slug wherever it can, so the colliding duplicate is the one that
moves, and the suffix is appended to that duplicate's own slug (a second
`overview-2` becomes `overview-2-2`, never `overview-3`). The familiar
`index-2.md` and `log-2.md` are what you get whenever those names are
otherwise free.

The rule carries two limits. First, a bumped path is not a stable identifier
across runs: `overview-2.md` belongs to whichever concept holds the lowest free
claim on the day of the export, so adding a source that owns `overview-2`
naturally moves the previous holder to `overview-3`. Key a consumer off a
concept's `resource:` field, not off its bundle filename. Second, a slug claim
is byte-exact and the filesystem underneath may not be. Two concepts of one
type whose slugs differ only in letter case, or only in Unicode normalization,
both keep their natural slug and are both written into the same type
directory, so on a case-insensitive or normalizing filesystem (the macOS
default) the second write lands on the first file and one concept's body is
gone. The reserved names are byte-exact for the same reason: a source named
`Index.md` keeps the slug `Index`, is written to `<type>/Index.md`, and is then
overwritten by the generated `<type>/index.md`. Both shapes exit `0`, and in
both the type index goes on listing a bullet whose target no longer holds what
the bullet says. Within one concept type, keep source stems distinct by more
than letter case or Unicode normalization.

**Every index lists exactly as many entries as it has things to list**, one
per line: a type index one entry per concept of that type, the root index one
per populated type directory. A type index is the only generated file that
puts source text into markdown, and the two fields it puts there (`title` and
`description`) are rendered as inline **text**, not as markup. Every control
character, line breaks included, becomes a space, and `[`, `]` and `\` are
backslash-escaped, so the entry stays on its own line and its link stays
inside the bundle. That is visible in the raw file: a title or description
containing a bracket now shows it as `\[`, which a markdown renderer displays
as the bare character.

Without that, a value could leave its own entry two ways, and neither one
raised: a newline broke the bullet across two lines, so the overflow read as
an entry the count above it did not admit (a newline reaches a description
from a `description: |` block scalar, or from a `\n` escape inside a
double-quoted one, which the parser now reads back); and an unescaped `]`
closed the link text early, handing the `(…)` behind it to the source as a
link destination without needing a newline at all. A generated index could
therefore list a fabricated entry pointing anywhere.

The guarantee is deliberately narrow: one entry per concept, whose link is
that concept's own file. Other inline markdown inside an entry (emphasis, an
autolink) still renders, exactly as it does inside a concept body, which is
markdown by design. The concept files need none of this: every source-derived
value there is written as an escaped double-quoted scalar.

Writes are **deterministic and idempotent**: re-running against unchanged
input produces a byte-identical file set, in a fresh interpreter too (concepts
are sorted by `(type, slug)`, the output directory is cleared and rebuilt on
every run, and nothing in the render depends on wall-clock time). The largest
way to lose that property is now refused rather than merely documented: an
`--out` inside a directory the chosen scope walks exits `1`, because the walk
would otherwise read the previous run's own output back in as source material
and the concept count would climb on every run. That guard is a strong default
and not a proof: see [the `--out` rules](#cli) for the destinations it does and
does not recognise.

## CLI

```bash
python3 scripts/okf-export.py --out dist/okf-bundle
python3 scripts/okf-export.py --root . --out dist/okf-bundle --scope core
python3 scripts/okf-export.py --out dist/okf-bundle --generated-by human:alice
```

| Flag | Default | Notes |
|---|---|---|
| `--root` | `.` | the Bridge instance root to export from |
| `--out` | *(required)* | the output bundle directory |
| `--scope` | `user` | `user` \| `core` — see [Scope](#scope) above |
| `--memory-dir` | *(derived)* | memory dir override — see [Memory](#memory) |
| `--generated-by` | `okf-export/<version>` | OKF actor for `generated.by` — see [Provenance and trust](#provenance-and-trust) |

Exit codes: `0` on success; `1` if `--root` does not exist or is not a
directory, if `--out` is `--root` or an ancestor of it, if `--out` points at
an existing non-bundle directory (the exporter refuses to clear anything that
does not look like a previous export), if `--out` sits inside a directory the
chosen scope walks (see below), if `--out` overlaps the memory dir a
`user`-scope run reads, or if `--generated-by` is not a valid OKF actor; an
unknown `--scope` value is rejected by `argparse` itself (`SystemExit`, exit
code `2`) before the exporter runs.

**`--out` must lie outside every scanned directory.** Under `core` scope that
means outside `docs/` and `examples/`; under `user` scope also outside `work/`
and `rules/`, and outside the memory dir. The refusal is raised before the walk
and before the destination is cleared, so a mistaken `--out` costs an error
message and nothing else, and it is derived from the same per-scope pattern
list the walk itself globs, so adding a source pattern extends the guard
automatically. That derivation takes each pattern's fixed leading path, which
makes the refusal slightly wider than the glob:
`work/**/deliverables/*.md` yields the prefix `work`, so a `user`-scope run
refuses all of `work/`, including subdirectories that hold no sources at all.

**Point `--out` outside what *every* scope walks, not just the current one.**
The guard knows only the scope of the run in front of it, so
`--out rules/bundle --scope core` is accepted (core walks `docs/` and
`examples/` only) and the next `--scope user` run then globs `rules/**/*.md`
and ingests that bundle as `rule` concepts. `dist/okf-bundle` is outside both
scopes and outside the memory dir, which is what makes it the documented
default.

The guard resolves the destination first, so `dist/../docs/bundle` is refused
exactly like `docs/bundle`. It compares that resolved path against the scanned
directories as spelled under `--root`, which leaves two spellings that reach a
scanned directory without matching it:

- a different letter case on a case-insensitive filesystem, e.g.
  `--out DOCS/okfbundle` under `core` scope on macOS;
- a scanned directory that is itself a symlink, e.g. a repo whose `docs/`
  points elsewhere.

Both are accepted and both then reproduce the self-ingestion in full: on a
small fixture the concept count runs 2, then 7, then 12 across three runs with
the sources unchanged. Neither spelling is reachable from the documented
`dist/okf-bundle`.

This is a **behaviour change** for every spelling the guard does recognise: a
command that pointed `--out` into a directory its own scope walks used to exit
`0` and quietly produce a corrupted bundle, each run re-ingesting the last.
It now exits `1`. Move `--out` to a directory outside the scanned tree.

## Migrating from v0.1

One intentional, consumer-visible break: **`timestamp` is gone**, superseded
by `generated.at`. It is not dual-emitted. The spec's legacy fallback applies
only when `generated` is absent, and `generated.by` is always written, so no
conformant v0.2 consumer would ever read it; carrying it would also re-ship
exactly those raw date strings the normalizer just refused. The bundle
self-declares its dialect via `okf_version`, which is the spec's own
mechanism for this.

Nothing else breaks. Bundles are regenerated by one command, `dist/` is
gitignored, and the `--out` guard still recognises a v0.1 bundle directory as
safe to clear, so a re-export over an existing bundle works unchanged.

`dist/` is gitignored (see `.gitignore`) — exported bundles are a build
artifact, and a `user`-scope bundle in particular may contain the same PII
as the `work/` tree it was exported from. Never commit an export.

## Tests

`scripts/tests/test_okf_export.py` (`bash scripts/tests/test-okf-export.sh`)
is a pytest suite in which every test builds its own synthetic mini-instance
under `tmp_path`, so no test reads or writes real instance content. Two tests
deliberately read repo files rather than fixtures: the exporter source (to
assert no wall-clock, git or environment call appears in the render path) and
this guide plus the suite's own docstring (to assert the documentation was
migrated alongside the code). Both read only; neither touches `work/` or a
memory directory.

The suite is the authoritative contract for every function's signature and
behaviour; this document describes intent and usage, the test file describes
the exact surface.
