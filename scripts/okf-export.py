#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Export a Bridge instance as an Open Knowledge Format (OKF) v0.2 bundle.

A single, additive, dependency-free script that walks a Bridge instance's
knowledge surfaces (work/ STATUS.md + deliverables, docs/, rules/,
examples/) and emits a static OKF bundle — one markdown file per concept, a
per-type index.md, and a root index.md carrying the OKF version. No PyYAML
dependency: frontmatter parsing is hand-rolled. It keeps
scripts/extract-frontmatter.py's leading-comment-prolog convention (skips the
`# yaml-language-server: $schema=...` hint) and reads the same flat
`key: value` scalars scripts/gen-board.py's parse_status() reads, but it
deliberately diverges from that script on two points, because gen-board.py is
lenient where YAML is not:

  * quoting is resolved BEFORE inline comments, the order YAML itself uses, so
    a `#` inside a quoted scalar stays a literal character (`"... as PR #214"`
    keeps its issue number). On an UNQUOTED scalar a ` #` still opens a
    comment and is still stripped.
  * a `---` fence is only a fence at COLUMN 0, for the opening and the closing
    one alike. A block-scalar continuation line is indented by definition, so
    an indented `---` is content and never ends the frontmatter block.

Concept mapping (source -> OKF `type`):
  work/tasks/<slug>/STATUS.md          -> task
  work/streams/<slug>/STATUS.md        -> stream
  work/done/<month>/<slug>/STATUS.md   -> task
  */deliverables/*.md under work/      -> deliverable
  docs/**/*.md                         -> doc
  rules/**/*.md                        -> rule
  examples/**/*.md                     -> example
  <memory-dir>/*.md fact files         -> memory   (user scope only)

Memory facts are the instance's auto-memory files (frontmatter with a
`name:` key); the directory usually lives OUTSIDE the repo and defaults to
`~/.claude/projects/<encoded-root>/memory` (override with --memory-dir).
Every concept carries a `resource:` field pointing at its source (repo-
relative path, or `memory/<filename>` for memory facts).

What v0.2 emits, and what it deliberately does not:

  generated.by   the actor that produced this BUNDLE DOCUMENT — by default
                 `okf-export/<EXPORTER_VERSION>`, overridable with
                 --generated-by. It never claims to be the author of the
                 underlying knowledge; `resource` is the provenance pointer.
  generated.at   the source's `last_updated`/`created`, normalized to an ISO
                 instant. A date that cannot be proven (partial, the literal
                 `YYYY-MM-DD` template placeholder, calendar-impossible) is
                 OMITTED rather than guessed.
  bridge_status  the source's Bridge workflow state, under a namespaced key.
                 It is NEVER written to OKF's own `status`: that field means
                 document readiness (draft|stable|deprecated) while a Bridge
                 status means work state (backlog|doing|review|done), and
                 `draft` is a homograph across the two. Absent `status`
                 already means `stable`, which is the true claim here.
  timestamp      REMOVED in v0.2 (superseded by generated.at). Not dual-emitted:
                 the spec's legacy fallback applies only when `generated` is
                 absent, which it never is.
  verified /     never emitted. Nothing in a Bridge instance is a verification
  sources /      event, a derivation edge, or an expiry instant, and all three
  stale_after    drive consumer behaviour (trust tiers, credibility
                 propagation, staleness gating) — so a fabricated value does
                 not read as noise, it reads as a false claim.

Empty optional fields are omitted, never written as "" or []: absence
carries meaning in OKF, so an empty value is a different claim from
"not recorded".

Wikilinks (kebab-case `[[slug]]` only — bash `[[ -f ... ]]` conditionals
never match) are resolved at export time against the bundle's own slug
index — never rewritten in the source repo. A resolved link becomes a
bundle-root-relative markdown link (`[slug](/<type>/<slug>.md)`, memory
concepts win cross-type slug collisions); an unresolved `[[slug]]` is left
completely untouched (OKF tolerates dangling references) and is reported
back in the manifest.

Scope controls which sources are walked:
  --scope user   everything (work/ + docs/ + rules/ + examples/ + memory)
                 — private, full-instance export.
  --scope core   docs/ + examples/ only — the public-safe subset for a demo
                 export (e.g. docs/ + examples/agency/). Run
                 scripts/no-scrub-leak.py over the output before publishing
                 a core-scope bundle.

--out must lie OUTSIDE every directory the chosen scope walks, and outside
the memory dir. A bundle written inside a scanned directory is read back in
as source material on the next run, so the concept count climbs on every run
and the byte-identical re-run guarantee is false; the destination is
therefore refused before the walk and before anything is cleared. The scanned
set is derived from the same per-scope pattern list discover_sources globs
(_SCOPE_PATTERNS), so it can never drift out of step with what is actually
read. dist/okf-bundle is outside the scanned set in both scopes.

Usage:
  python3 scripts/okf-export.py --out dist/okf-bundle
  python3 scripts/okf-export.py --root . --out dist/okf-bundle --scope core
  python3 scripts/okf-export.py --out dist/okf-bundle --generated-by human:alice

Exit codes:
  0 — bundle written successfully
  1 — --root does not exist / is not a directory, unsafe --out refused, or
      --generated-by is not a valid OKF actor. An --out is unsafe when it is
      --root or an ancestor of it, when it sits inside a scanned directory or
      the memory dir, or when it is a non-empty directory that does not look
      like a prior bundle.
  2 — argparse usage error (e.g. unknown --scope; raised as SystemExit)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

OKF_VERSION = "0.2"
# The exporter's OWN version, deliberately separate from the spec version so
# the two can never drift into each other. Hand-bumped: deriving it from
# `git describe` would make the output depend on clone state and break the
# byte-identical re-run guarantee.
EXPORTER_VERSION = "1.0"

# OKF section 7 actor convention: `<producer>/<version>` for an agent or tool,
# `human:<id>` for a person, `process:<id>` for an automated process. The value
# is written verbatim into every concept's `generated.by`, so it is validated
# rather than trusted: an unconstrained string is an unreadable provenance
# claim, and one containing a newline or a quote is malformed YAML. (Trust
# TIERS derive from `verified`, not from this field, so a wrong actor here
# misattributes provenance but cannot inflate a tier.)
# `\A`/`\Z`, never `^`/`$`: in Python `$` also matches before a trailing
# newline, so `--generated-by $'human:alice\n'` would pass a `$`-anchored gate
# and then split the rendered `generated:` flow mapping across two lines.
_ACTOR_RE = re.compile(r"\A(?:human|process):[A-Za-z0-9._-]+\Z|\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
# Only a full calendar date. `date.fromisoformat` also accepts `20260702` and
# `2026-W27-1` on Python 3.11+, which must NOT be silently widened.
_BARE_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
# An instant that already carries an explicit UTC offset, as OKF section 5
# requires. `T` only: a space separator is not the ISO 8601 form the spec names.
_OFFSET_DATETIME_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})\Z"
)

# Kebab-case identifiers only: bash `[[ -f file ]]` conditionals inside code
# blocks must never match (and must never be rewritten or reported).
_WIKILINK_RE = re.compile(r"\[\[([a-z][a-z0-9-]*)\]\]")
_FRONTMATTER_KV_RE = re.compile(r"^([A-Za-z_][\w]*):\s*(.*)$")
_YAML_LS_PROLOG_RE = re.compile(r"^#\s*yaml-language-server:")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*$")
# An inline comment on an UNQUOTED scalar. The `#` must open the line or follow
# whitespace, exactly as YAML requires: `value#nospace` is one plain scalar, and
# `key: # note` (the hash directly after the `key:` separator, so the value is
# empty) is a comment. Applied only after quoting has been resolved.
_PLAIN_COMMENT_RE = re.compile(r"(?:\A|\s)#.*\Z")
# The escapes `_yaml_quote` emits, read back. Anything else is left verbatim
# rather than guessed at, so an unrelated backslash pair survives untouched.
_DQ_ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "r": "\r", "t": "\t"}
_RESERVED_SLUGS = frozenset({"index", "log"})
# A slug becomes a FILENAME, so it must be one path segment and nothing else.
# Repo-derived slugs come from the filesystem and are safe by construction, but
# a memory fact's `name:` is arbitrary frontmatter: `../../x` or `sub/dir` would
# make the exporter write outside --out, and it is supposed to only ever read
# the source tree. Leading dot excluded, which also rules out `.` and `..`.
_SAFE_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
# Memory-dir housekeeping files that are never concepts (index + provenance).
_MEMORY_SKIP = frozenset({"MEMORY.md", "MEMORY-ARCHIVE.md", "PROVENANCE.md"})


def _resolve_scalar(raw: str) -> tuple[str, bool]:
    """Resolve one frontmatter value. Returns (value, was_quoted).

    Quoting is resolved FIRST and the inline comment second, which is the
    order YAML itself uses: a `#` opens a comment only outside a quoted
    scalar. Doing it the other way round truncates `"... as PR #214"` at the
    hash and then removes the orphaned opening quote, so the loss leaves no
    trace for the caller to notice.

    Three paths:

    * double-quoted: scan to the closing `"`, honouring backslash escapes so
      `\\"` does not close the scalar. Exactly the set ``_yaml_quote`` emits is
      unescaped, so a value this exporter wrote survives a write/read round
      trip; any other backslash pair is left verbatim rather than guessed at,
      which is what keeps a Windows path (`C:\\path\\to`) intact. Everything
      after the closing quote is the comment position and is discarded.
    * single-quoted: scan to the closing `'`, collapsing a doubled `''` to one
      `'` (the only escape a YAML single-quoted scalar has).
    * plain, or a quote that is never closed: strip a trailing inline comment,
      then apply the historic quote strip so a malformed value degrades
      exactly as it did before.

    ``was_quoted`` is load-bearing rather than informational: with quotes
    resolved first, `title: "|"` would otherwise reach the block-scalar check
    as a bare `|` and swallow the following frontmatter lines as its body.
    """
    raw = raw.rstrip()  # also disposes of a trailing \r on a CRLF source

    if raw.startswith('"'):
        out: list[str] = []
        idx = 1
        while idx < len(raw):
            ch = raw[idx]
            if ch == "\\" and idx + 1 < len(raw):
                unescaped = _DQ_ESCAPES.get(raw[idx + 1])
                out.append(unescaped if unescaped is not None else raw[idx : idx + 2])
                idx += 2
                continue
            if ch == '"':
                return "".join(out), True
            out.append(ch)
            idx += 1
    elif raw.startswith("'"):
        out = []
        idx = 1
        while idx < len(raw):
            ch = raw[idx]
            if ch == "'":
                if idx + 1 < len(raw) and raw[idx + 1] == "'":
                    out.append("'")
                    idx += 2
                    continue
                return "".join(out), True
            out.append(ch)
            idx += 1

    value = _PLAIN_COMMENT_RE.sub("", raw, count=1).strip()
    return value.strip('"').strip("'"), False


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``text`` into (frontmatter dict, body). Hand-rolled, no PyYAML.

    Reads the same flat `key: value` scalars as scripts/gen-board.py's
    parse_status() and keeps scripts/extract-frontmatter.py's
    leading-comment-prolog skip (so a `# yaml-language-server: $schema=...`
    hint above the `---` fence never confuses detection), with two deliberate
    divergences from gen-board.py: quoting is resolved before inline comments
    (see ``_resolve_scalar``), and a `---` fence counts only at column 0, for
    the opening and the closing fence alike. A file with no frontmatter block
    returns ({}, text) with the body left completely untouched.
    """
    lines = text.splitlines(keepends=True)
    in_block = False
    fm_start_idx: int | None = None
    fence_close_idx: int | None = None

    for idx, line in enumerate(lines):
        if not in_block:
            if _YAML_LS_PROLOG_RE.match(line.lstrip()):
                continue
            # `rstrip`, never `strip`: a fence lives at column 0. Trailing
            # whitespace (and a CRLF `\r`) is tolerated, leading indentation is
            # not, so an indented `---` stays content.
            if line.rstrip() == "---":
                in_block = True
                fm_start_idx = idx + 1
                continue
            if line.strip() == "":
                continue
            # first non-empty, non-comment, non-fence line -> no frontmatter block
            return {}, text
        else:
            # Same column-0 rule as the opening fence: a block-scalar
            # continuation line is indented by definition, so an indented
            # `---` inside one must not close the block.
            if line.rstrip() == "---":
                fence_close_idx = idx
                break

    if fm_start_idx is None or fence_close_idx is None:
        return {}, text

    fm: dict[str, str] = {}
    fm_lines = lines[fm_start_idx:fence_close_idx]
    idx2 = 0
    while idx2 < len(fm_lines):
        line = fm_lines[idx2]
        stripped = line.rstrip("\n")
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            idx2 += 1
            continue
        m = _FRONTMATTER_KV_RE.match(stripped)
        if not m:
            idx2 += 1
            continue
        key, val = m.group(1), m.group(2)
        val, was_quoted = _resolve_scalar(val)
        # A quoted value is a string, never an indicator: `title: "|"` is the
        # one-character string, not the head of a block scalar.
        if not was_quoted and _BLOCK_SCALAR_RE.match(val):
            # YAML block scalar (`>-`/`|`/...) — fold/preserve the indented
            # continuation lines instead of shipping the bare indicator as
            # the literal value.
            folded = val.startswith(">")
            idx2 += 1
            block_lines: list[str] = []
            while idx2 < len(fm_lines) and (
                fm_lines[idx2].strip() == "" or fm_lines[idx2].startswith((" ", "\t"))
            ):
                block_line = fm_lines[idx2].rstrip("\n").strip()
                if block_line:
                    block_lines.append(block_line)
                idx2 += 1
            val = " ".join(block_lines) if folded else "\n".join(block_lines)
        else:
            idx2 += 1
        fm[key] = val

    body = "".join(lines[fence_close_idx + 1 :])
    return fm, body


def concept_slug(path: Path) -> str:
    """`STATUS.md` -> parent directory name; anything else -> the file stem."""
    if path.name == "STATUS.md":
        return path.parent.name
    return path.stem


def derive_title(frontmatter: dict, body: str, fallback: str) -> str:
    """frontmatter["title"] -> first `# ` H1 line in body -> fallback."""
    title = frontmatter.get("title")
    if title:
        return title
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def derive_description(frontmatter: dict, body: str) -> str:
    """frontmatter["description"] -> ["summary"] -> ["headline"] -> "" (never from body)."""
    del body  # part of the documented signature; description never derives from body
    for key in ("description", "summary", "headline"):
        value = frontmatter.get(key)
        if value:
            return value
    return ""


def resolve_wikilinks(text: str, slug_to_relpath: dict) -> tuple[str, list[str]]:
    """Replace every kebab-case `[[slug]]`: resolved -> markdown link,
    unresolved -> left completely untouched (OKF tolerates dangling
    references; rewriting them would corrupt content such as bash
    conditionals or deliberate wiki syntax).

    Returns (new_text, unresolved_slugs) — unresolved slugs are reported in
    the order they were encountered (duplicates included, callers dedupe).
    """
    unresolved: list[str] = []

    def _replace(match: re.Match) -> str:
        slug = match.group(1)
        relpath = slug_to_relpath.get(slug)
        if relpath is not None:
            return f"[{slug}]({relpath})"
        unresolved.append(slug)
        return match.group(0)

    new_text = _WIKILINK_RE.sub(_replace, text)
    return new_text, unresolved


# The ONE list of source patterns per scope, read by BOTH consumers:
# discover_sources (which globs them) and scanned_dirs (which derives from
# them the directories write_bundle refuses to write a bundle into). A second
# literal copy in the guard would drift silently the day someone adds a
# pattern to only one of the two, and the walk would resume eating its own
# output (issue #153). Add a pattern here and both behaviours follow.
_SCOPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "core": (
        "docs/**/*.md",
        "examples/**/*.md",
    ),
    "user": (
        "work/tasks/*/STATUS.md",
        "work/streams/*/STATUS.md",
        "work/done/*/*/STATUS.md",
        "work/**/deliverables/*.md",
        "docs/**/*.md",
        "rules/**/*.md",
        "examples/**/*.md",
    ),
}

# The glob metacharacters Path.glob honours. A segment containing any of them
# is not a fixed directory name, so prefix derivation stops there.
_GLOB_CHARS = frozenset("*?[")


def patterns_for(scope: str) -> tuple[str, ...]:
    """The source glob patterns for ``scope`` ("user" or "core")."""
    try:
        return _SCOPE_PATTERNS[scope]
    except KeyError:
        expected = " or ".join(repr(name) for name in sorted(_SCOPE_PATTERNS))
        raise ValueError(f"unknown scope: {scope!r} (expected {expected})") from None


def _pattern_prefix(pattern: str) -> str:
    """The fixed leading path of a glob pattern, up to its first globbed segment.

    ``docs/**/*.md`` -> ``docs``; ``work/tasks/*/STATUS.md`` -> ``work/tasks``;
    ``work/**/deliverables/*.md`` -> ``work``. Segment-wise by construction,
    never a string prefix, so a sibling named ``docs-bundle`` is not mistaken
    for something inside ``docs``.

    A pattern whose very first segment is globbed has NO fixed prefix and
    yields "". The walk then reads the whole tree, which scanned_dirs turns
    into the root itself rather than dropping the pattern and under-guarding.
    """
    fixed: list[str] = []
    for segment in pattern.split("/"):
        if _GLOB_CHARS & set(segment):
            break
        fixed.append(segment)
    return "/".join(fixed)


def scanned_dirs(root: Path, scope: str) -> list[Path]:
    """The resolved directories ``discover_sources(root, scope)`` reads under.

    Derived from ``patterns_for(scope)``, never from a hand-kept second list.
    Resolved, so a symlinked or dot-segmented path is compared on its real
    location. A directory that does not exist yet is still returned: it is
    scanned as soon as it appears, and a bundle sitting there would be read
    back in on the next run.
    """
    root = Path(root).resolve()
    prefixes = {_pattern_prefix(pattern) for pattern in patterns_for(scope)}
    return sorted({(root / prefix) if prefix else root for prefix in prefixes})


def discover_sources(root: Path, scope: str) -> list[Path]:
    """Walk ``root`` for OKF source files per ``scope`` ("user" or "core")."""
    root = Path(root)
    found: set[Path] = set()
    for pattern in patterns_for(scope):
        found.update(p for p in root.glob(pattern) if p.is_file())
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def concept_type_for(path: Path, root: Path) -> str:
    """Map a discovered source path to its OKF concept type."""
    rel = Path(path).relative_to(root)
    parts = rel.parts

    if parts[0] == "work":
        if "deliverables" in parts:
            return "deliverable"
        if path.name == "STATUS.md" and len(parts) >= 2:
            if parts[1] == "tasks":
                return "task"
            if parts[1] == "streams":
                return "stream"
            if parts[1] == "done":
                return "task"
    elif parts[0] == "docs":
        return "doc"
    elif parts[0] == "rules":
        return "rule"
    elif parts[0] == "examples":
        return "example"

    raise ValueError(f"cannot determine OKF concept type for {rel}")


def default_generated_by() -> str:
    """The actor this exporter names as the producer of a bundle document.

    It names the TRANSFORMATION, never the author of the underlying
    knowledge: the exporter produced this OKF file, it did not write the
    Bridge document behind it. `resource` is the real provenance pointer.
    Deriving an author from `git log`/`git config`/`$USER` is refused on
    three counts: a committer is not a knowledge author, it would inject a
    real identity into a `--scope core` bundle, and it would make output
    depend on clone state.
    """
    return f"okf-export/{EXPORTER_VERSION}"


def normalize_timestamp(value: str) -> str | None:
    """Coerce a source date to an OKF instant, or return None if unprovable.

    OKF section 5 requires every timestamp-valued key to be an ISO 8601
    datetime with an explicit UTC offset, but Bridge sources carry bare
    dates (``last_updated: 2026-07-02``). Exactly two shapes are accepted:

    * a full calendar date -> widened to ``<date>T00:00:00Z``. Midnight is
      the EARLIEST instant consistent with the stated date, so no consumer
      ever reads the content as fresher than the evidence supports.
    * a datetime that already carries an offset -> passed through verbatim.

    Everything else returns None and the caller omits ``generated.at``
    entirely: a partial date (``2026-03``) would require inventing a day,
    ``work/templates/STATUS.md`` seeds new tasks with the literal
    ``YYYY-MM-DD`` placeholder, and section 5.2 does not mark ``at``
    required. Pure: reads only its argument, never the clock.

    Both branches PROVE the value rather than merely shape-matching it: the
    regexes are all ``\\d{2}`` groups, so month 13, day-31-in-June and hour 25
    match the pattern. An impossible instant, emitted unquoted, is frontmatter
    no YAML parser can load, which breaks conformance for the whole bundle.
    """
    value = (value or "").strip()
    if _BARE_DATE_RE.match(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return None  # calendar-impossible, e.g. 2026-02-30
        return f"{value}T00:00:00Z"
    if _OFFSET_DATETIME_RE.match(value):
        # `Z` is only accepted by fromisoformat from 3.11; normalize for the
        # probe so the check does not depend on the interpreter version.
        probe = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            datetime.fromisoformat(probe)
        except ValueError:
            return None  # e.g. 2026-06-31T09:00:00Z, 2026-07-02T25:00:00Z
        return value
    return None


def build_concept(path: Path, root: Path) -> dict:
    """Read ``path`` and build its OKF concept dict (body left un-resolved)."""
    text = Path(path).read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    slug = concept_slug(path)
    okf_type = concept_type_for(path, root)
    title = derive_title(fm, body, fallback=slug)
    description = derive_description(fm, body)
    generated_at = normalize_timestamp(fm.get("last_updated") or fm.get("created") or "")
    tags = [value for value in (fm.get("status"), fm.get("context")) if value]
    return {
        "slug": slug,
        "okf_type": okf_type,
        "title": title,
        "description": description,
        "resource": Path(path).relative_to(root).as_posix(),
        "generated_at": generated_at,
        # A Bridge workflow state, NEVER OKF's `status`. The two vocabularies
        # are orthogonal and `draft` is a homograph across them: see the
        # module docstring.
        "bridge_status": fm.get("status") or None,
        "tags": tags,
        "body": body,
    }


def default_memory_dir(root: Path) -> Path:
    """Derive the instance's auto-memory directory from its root path.

    The harness stores per-project memory under
    ``~/.claude/projects/<encoded>/memory`` where ``<encoded>`` is the
    absolute project path with every ``/`` replaced by ``-`` (the leading
    slash becomes a leading dash).
    """
    encoded = str(Path(root).resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"


def discover_memory(memory_dir: Path) -> list[Path]:
    """Memory fact files: every ``*.md`` with a ``name:`` frontmatter key,
    excluding index/provenance housekeeping files (``MEMORY.md`` etc.)."""
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(memory_dir.glob("*.md"), key=lambda p: p.name):
        if path.name in _MEMORY_SKIP or path.name.startswith("_"):
            continue
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fm.get("name"):
            found.append(path)
    return found


def build_memory_concept(path: Path) -> dict:
    """Build a ``memory``-type concept from an auto-memory fact file.

    Slug = the frontmatter ``name:`` (already kebab-case by convention),
    falling back to the filename stem with its ``<type>_`` prefix stripped
    and underscores dashed. Memory facts carry no top-level date, so
    ``generated_at`` stays None and the key is omitted rather than guessed;
    ``resource`` points into the (out-of-repo) memory dir.
    """
    text = Path(path).read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    # The filename-derived slug is safe by construction (a directory entry can
    # hold no separator), so it doubles as the fallback when `name:` is absent
    # OR is not a usable single path segment.
    fallback = path.stem.split("_", 1)[-1].replace("_", "-")
    candidate = fm.get("name") or fallback
    slug = candidate if _SAFE_SLUG_RE.match(candidate) else fallback
    return {
        "slug": slug,
        "okf_type": "memory",
        "title": derive_title(fm, body, fallback=slug),
        "description": derive_description(fm, body),
        "resource": f"memory/{path.name}",
        "generated_at": None,
        "bridge_status": None,
        "tags": [],
        "body": body,
    }


_YAML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _yaml_quote(value: str) -> str:
    """Render a scalar as a double-quoted YAML string, safe on ONE line.

    A double-quoted scalar is the only flow form that can carry an arbitrary
    string, but only if everything that would end the scalar or the line is
    escaped. Escaping just backslash and quote is not enough: a raw newline
    folds the value into a space, or — when the continuation line happens to
    start with ``---`` — terminates the frontmatter block outright, and a raw
    control character makes the whole block unreadable to a real YAML parser.
    Source frontmatter reaches this function verbatim (a ``title: |`` block
    scalar arrives multi-line), so it must survive anything a source file
    can hold.
    """
    out: list[str] = []
    for ch in value:
        escaped = _YAML_ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ch < "\x20" or ch == "\x7f" or "\x80" <= ch <= "\x9f":
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _render_concept(concept: dict, generated_by: str | None = None) -> str:
    """Render one OKF v0.2 concept file.

    An empty optional field is OMITTED rather than written as ``""``/``[]``:
    OKF section 5 states that absence carries meaning, so an empty string is
    a different (and false) claim from "not recorded". Only ``type`` is
    always required (section 4.1).
    """
    generated_by = generated_by or default_generated_by()
    lines = [
        "---",
        f"type: {concept['okf_type']}",
        f"title: {_yaml_quote(concept['title'])}",
    ]
    if concept["description"]:
        lines.append(f"description: {_yaml_quote(concept['description'])}")
    lines.append(f"resource: {_yaml_quote(concept['resource'])}")

    # `by` is the only REQUIRED key inside `generated` (section 5.2); `at` is
    # written only when the source date could be proven (see normalize_timestamp).
    generated = f"{{ by: {generated_by}"
    if concept.get("generated_at"):
        generated += f", at: {concept['generated_at']}"
    lines.append(f"generated: {generated} }}")

    if concept.get("bridge_status"):
        lines.append(f"bridge_status: {_yaml_quote(concept['bridge_status'])}")
    if concept["tags"]:
        # Every tag is quoted individually: an unquoted flow sequence splits a
        # tag on its own comma and is broken outright by a `]` inside a value.
        tags = ", ".join(_yaml_quote(tag) for tag in concept["tags"])
        lines.append(f"tags: [{tags}]")
    lines.extend(["---", ""])
    return "\n".join(lines) + concept["body"]


def _render_type_index(okf_type: str, concepts: list[dict]) -> str:
    """Render a per-type ``index.md`` — a reserved filename, so it carries NO
    frontmatter block (the root ``index.md`` is the sole exception, carrying
    ``okf_version``)."""
    lines = [
        f"# {okf_type.capitalize()} Index",
        "",
        f"{len(concepts)} concept(s).",
        "",
    ]
    # `* [Title](relative-url) - short description` is the form OKF section 8
    # shows for an index entry.
    for concept in sorted(concepts, key=lambda c: c["slug"]):
        suffix = f" - {concept['description']}" if concept["description"] else ""
        lines.append(f"* [{concept['title']}]({concept['slug']}.md){suffix}")
    lines.append("")
    return "\n".join(lines)


def _render_root_index(scope: str, concepts: list[dict], types: list[str]) -> str:
    """Render the bundle-root ``index.md``.

    ``okf_version`` is the ONE key OKF permits in an index file's
    frontmatter (sections 8 + 12), so scope and concept count are stated in
    the body prose instead of the block. ``_is_bundle_dir`` keys off
    ``okf_version`` alone, so the destructive ``--out`` guard still
    recognises bundles written by earlier versions.
    """
    lines = [
        "---",
        f"okf_version: {_yaml_quote(OKF_VERSION)}",
        "---",
        "",
        "# OKF Bundle",
        "",
        f"Open Knowledge Format v{OKF_VERSION} export. Scope: {scope}. "
        f"{len(concepts)} concept(s).",
        "",
    ]
    for okf_type in types:
        lines.append(f"* [{okf_type}]({okf_type}/index.md)")
    lines.append("")
    return "\n".join(lines)


class BundleDestinationError(Exception):
    """Raised when ``--out`` is not safe to clear/write."""


def _is_bundle_dir(path: Path) -> bool:
    """True if ``path`` looks like a prior OKF bundle (root index.md carrying
    ``okf_version`` frontmatter) — i.e. safe to clear and regenerate."""
    index = path / "index.md"
    if not index.is_file():
        return False
    fm, _ = parse_frontmatter(index.read_text(encoding="utf-8"))
    return "okf_version" in fm


def _slug_is_taken(taken: set[tuple[str, str]], okf_type: str, slug: str) -> bool:
    """True when ``slug`` is unavailable for a concept of ``okf_type``.

    The ONE predicate both passes of dedupe_slugs ask, so a reserved name and
    a claimed name can never be checked asymmetrically. The reserved test is
    deliberately type-independent: ``<type>/index.md`` is generated for every
    populated type directory, so no type may ever hold a concept named after
    a reserved file, and a future reserved name such as "log-2" is honoured
    by the bump loop for free.
    """
    return slug in _RESERVED_SLUGS or (okf_type, slug) in taken


def dedupe_slugs(concepts: list[dict]) -> None:
    """Ensure every concept's (okf_type, slug) is unique and never collides
    with a reserved OKF filename. Mutates ``concept["slug"]`` in place.

    "index" is always reserved: write_bundle generates ``<type>/index.md``
    itself, so any source concept slugged "index" would otherwise be
    silently clobbered by it. "log" is reserved for a chronological
    change-history file. Both, plus any other same-type slug collision
    (e.g. two differently-pathed ``README.md`` sources), take the LOWEST FREE
    numeric suffix (``slug-2``, ``slug-3``, ...), checked against every
    natural claim in the bundle rather than against a per-stem counter.

    A concept keeps its own natural slug wherever it can; the colliding
    duplicate is the one that moves. That rule needs TWO passes, because the
    natural owner of a bumped name may be discovered AFTER the duplicate that
    would otherwise be handed it (issue #152: docs/api/overview.md,
    docs/cli/overview.md, docs/zz/overview-2.md). Pass 1 claims every natural
    slug; only then does pass 2 compute a suffix, so a bumped name can never
    land on a slug some other concept owns, whichever order they arrive in.

    The suffix is appended to the concept's OWN natural slug, never to a
    parsed-off stem: a duplicate of a natural "overview-2" becomes
    "overview-2-2" rather than stealing "overview-3" from its potential
    natural owner.

    Deterministic: both passes walk ``concepts`` in discovery order (the list
    arrives pre-sorted by source path) and each candidate suffix is tried
    from 2 upward, so the assignment is fully determined by the input list.
    ``taken`` is a membership set only and is never iterated, so no set
    ordering can reach a filename.
    """
    taken: set[tuple[str, str]] = set()
    pending: list[dict] = []

    # Pass 1 (claim): every concept that CAN keep its natural slug does, and
    # the rest are parked. `taken` is complete before any suffix is computed.
    for concept in concepts:
        okf_type, slug = concept["okf_type"], concept["slug"]
        if _slug_is_taken(taken, okf_type, slug):
            pending.append(concept)
        else:
            taken.add((okf_type, slug))

    # Pass 2 (bump): the lowest free suffix on the concept's own stem.
    for concept in pending:
        okf_type, stem = concept["okf_type"], concept["slug"]
        suffix = 2
        while _slug_is_taken(taken, okf_type, f"{stem}-{suffix}"):
            suffix += 1
        concept["slug"] = f"{stem}-{suffix}"
        taken.add((okf_type, concept["slug"]))


def _assert_unique_slugs(concepts: list[dict]) -> None:
    """Belt and braces behind dedupe_slugs: no two concepts of one type may
    share a slug, because ``write_text`` would silently let the second win.

    The failure this guards is invisible in both directions (issue #152): the
    bundle holds one fewer file than the manifest counts, and the type index
    grows two bullets pointing at one file. Raising ``BundleDestinationError``
    rather than a new exception type is deliberate, so main() already turns it
    into a clean stderr line and exit 1 instead of a traceback.
    """
    claimed: dict[tuple[str, str], str] = {}
    for concept in concepts:
        key = (concept["okf_type"], concept["slug"])
        first = claimed.get(key)
        if first is not None:
            raise BundleDestinationError(
                f"refusing to write the bundle: two {key[0]} concepts both "
                f"resolve to the slug {key[1]!r} ({first} and "
                f"{concept['resource']}), so one would overwrite the other"
            )
        claimed[key] = concept["resource"]


def write_bundle(
    root: Path,
    out_dir: Path,
    scope: str,
    memory_dir: Path | None = None,
    generated_by: str | None = None,
) -> dict:
    """Discover -> build -> resolve wikilinks -> write an OKF bundle at ``out_dir``.

    ``memory_dir`` (user scope only) adds the instance's auto-memory fact
    files as ``memory``-type concepts — the primary wikilink target.

    ``generated_by`` is the run-wide OKF actor written into every concept's
    ``generated.by``; it defaults to this exporter (see
    ``default_generated_by``). Run-wide rather than per-concept on purpose,
    so ``build_concept`` stays a pure function of the source file's bytes.

    Deterministic and idempotent: re-running against unchanged input produces
    a byte-identical file set (stable sort order, no wall-clock content).
    """
    generated_by = generated_by or default_generated_by()
    root = Path(root).resolve()
    out_dir = Path(out_dir).resolve()

    if out_dir == root or root.is_relative_to(out_dir):
        raise BundleDestinationError(
            f"refusing to write the bundle to {out_dir}: it is --root or an "
            "ancestor of --root, so clearing it would delete source data, "
            "not just the bundle"
        )

    # The converse guard, and the reason it runs HERE: a destination inside a
    # directory this scope walks would be read back in as source material on
    # the very next run, so the bundle would grow a concept per generated file
    # every time and the byte-identical re-run guarantee would be false. It
    # has to fire before discover_sources below (or the previous run's output
    # is already in memory) and before the rmtree further down (or the
    # evidence is deleted by the same run that consumed it).
    for scanned in scanned_dirs(root, scope):
        if out_dir.is_relative_to(scanned):
            raise BundleDestinationError(
                f"refusing to write the bundle to {out_dir}: it is inside "
                f"{scanned}, which --scope {scope} walks for sources, so every "
                "run would re-ingest the previous run's output as source "
                "material. Point --out at a directory no scope walks "
                "(e.g. dist/okf-bundle)"
            )
        if scanned.is_relative_to(out_dir):
            raise BundleDestinationError(
                f"refusing to write the bundle to {out_dir}: it contains "
                f"{scanned}, which --scope {scope} walks for sources, so "
                "clearing it would delete source data, not just the bundle"
            )

    # The memory dir is read ONLY under user scope (see the discover_memory
    # call below), and this clause mirrors that read exactly rather than
    # exceeding it. The store usually lives outside the repo and the exporter
    # is strictly a reader of it, so a bundle written there would put an
    # rmtree on someone else's data every run. The non-bundle guard further
    # down only covers this by accident, and not at all when the memory dir is
    # empty or already holds a bundle.
    if scope == "user" and memory_dir is not None:
        resolved_memory = Path(memory_dir).resolve()
        if out_dir.is_relative_to(resolved_memory) or resolved_memory.is_relative_to(out_dir):
            raise BundleDestinationError(
                f"refusing to write the bundle to {out_dir}: it overlaps the "
                f"memory dir {resolved_memory}, which this run reads as a "
                "source. The exporter never writes into the memory store"
            )

    sources = discover_sources(root, scope)
    concepts = [build_concept(path, root) for path in sources]
    if scope == "user" and memory_dir is not None:
        concepts.extend(build_memory_concept(path) for path in discover_memory(memory_dir))
    dedupe_slugs(concepts)
    # Before the sort, before the rmtree below, and before any write: a future
    # regression must fail with the previous bundle intact, not after it.
    _assert_unique_slugs(concepts)
    concepts.sort(key=lambda c: (c["okf_type"], c["slug"]))

    # Wikilinks are memory references by convention — on a cross-type slug
    # collision the memory concept wins the link target, everything else is
    # first-come in the stable (type, slug) order.
    slug_to_relpath: dict[str, str] = {}
    for concept in concepts:
        if concept["okf_type"] == "memory":
            slug_to_relpath[concept["slug"]] = f"/memory/{concept['slug']}.md"
    for concept in concepts:
        slug_to_relpath.setdefault(
            concept["slug"], f"/{concept['okf_type']}/{concept['slug']}.md"
        )

    unresolved_all: set[str] = set()
    for concept in concepts:
        resolved_body, unresolved = resolve_wikilinks(concept["body"], slug_to_relpath)
        concept["body"] = resolved_body
        unresolved_all.update(unresolved)

    if out_dir.exists():
        if any(out_dir.iterdir()) and not _is_bundle_dir(out_dir):
            raise BundleDestinationError(
                f"refusing to clear {out_dir}: it already exists, is "
                "non-empty, and does not look like a prior OKF bundle (no "
                "index.md carrying okf_version) — point --out at an empty "
                "directory or a previous bundle output"
            )
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_type: dict[str, list[dict]] = {}
    for concept in concepts:
        by_type.setdefault(concept["okf_type"], []).append(concept)

    for okf_type in sorted(by_type):
        type_concepts = by_type[okf_type]
        type_dir = out_dir / okf_type
        type_dir.mkdir(parents=True, exist_ok=True)
        for concept in sorted(type_concepts, key=lambda c: c["slug"]):
            # Belt and braces behind _SAFE_SLUG_RE: whatever a future slug
            # source is, a concept must never be written outside its own type
            # directory. The exporter is read-only against the source tree, and
            # an escaping filename is precisely how that guarantee would break.
            target = type_dir / f"{concept['slug']}.md"
            if target.parent.resolve() != type_dir.resolve():
                raise BundleDestinationError(
                    f"refusing to write concept {concept['slug']!r}: it resolves "
                    f"outside {type_dir}"
                )
            target.write_text(_render_concept(concept, generated_by), encoding="utf-8")
        (type_dir / "index.md").write_text(
            _render_type_index(okf_type, type_concepts), encoding="utf-8"
        )

    (out_dir / "index.md").write_text(
        _render_root_index(scope, concepts, sorted(by_type)), encoding="utf-8"
    )

    return {
        "okf_version": OKF_VERSION,
        "scope": scope,
        "concept_count": len(concepts),
        "generated_by": generated_by,
        # A COUNT, never a list of paths: the manifest is printed, and a list
        # would put instance-relative source paths into that output.
        "concepts_without_generated_at": sum(
            1 for c in concepts if not c.get("generated_at")
        ),
        "unresolved_wikilinks": sorted(unresolved_all),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="okf-export.py",
        description="Export a Bridge instance as an Open Knowledge Format (OKF) v0.2 bundle.",
    )
    parser.add_argument("--root", default=".", help="Bridge instance root (default: .)")
    parser.add_argument("--out", required=True, help="output bundle directory")
    parser.add_argument(
        "--scope",
        choices=["user", "core"],
        default="user",
        help="user = everything (work/+docs/+rules/+examples/+memory); "
        "core = docs/+examples/ only (default: user)",
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help="auto-memory directory to export as memory concepts (user scope "
        "only; default: derived as ~/.claude/projects/<encoded-root>/memory, "
        "silently skipped when absent)",
    )
    parser.add_argument(
        "--generated-by",
        default=None,
        metavar="ACTOR",
        help="OKF actor written to every concept's generated.by; one of "
        "'<producer>/<version>', 'human:<id>' or 'process:<id>' "
        f"(default: {default_generated_by()})",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        sys.stderr.write(f"ERROR: --root {root} does not exist or is not a directory\n")
        return 1

    generated_by = args.generated_by or default_generated_by()
    if not _ACTOR_RE.match(generated_by):
        sys.stderr.write(
            f"ERROR: --generated-by {generated_by!r} is not an OKF actor. Use "
            "'<producer>/<version>', 'human:<id>' or 'process:<id>'. The value "
            "is written verbatim into every concept, so an unconstrained string "
            "would be an unreadable provenance claim at best and malformed YAML "
            "at worst.\n"
        )
        return 1
    if generated_by.startswith("human:") and args.scope == "core":
        sys.stderr.write(
            f"WARNING: --scope core with a human actor ({generated_by}) writes a "
            "personal identifier into every concept of a bundle whose whole "
            "point is being publishable. Proceeding as requested.\n"
        )

    memory_dir: Path | None = None
    if args.scope == "user":
        memory_dir = Path(args.memory_dir) if args.memory_dir else default_memory_dir(root)
        if not memory_dir.is_dir():
            sys.stderr.write(f"NOTICE: no memory dir at {memory_dir} — skipping memory export\n")
            memory_dir = None

    out_dir = Path(args.out)
    try:
        manifest = write_bundle(
            root, out_dir, args.scope, memory_dir=memory_dir, generated_by=generated_by
        )
    except BundleDestinationError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    print(
        f"okf-export: wrote {manifest['concept_count']} concept(s) to {out_dir} "
        f"(scope={manifest['scope']}, okf_version={manifest['okf_version']}, "
        f"generated_by={manifest['generated_by']}, "
        f"without_generated_at={manifest['concepts_without_generated_at']}, "
        f"unresolved_wikilinks={len(manifest['unresolved_wikilinks'])})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
