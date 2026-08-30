#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A table of contents for a declared map source, and one block on demand.

Shared by `scripts/context-index.py` (the CLI a session calls) and
`scripts/measure-context.py` (which measures the card rather than the file
once one is declared). The contract is specified by
`scripts/tests/test_context_index.py`.

TWO DELIBERATE CHOICES, both cost something and both are the point.

SLICES ARE RAW TEXT. This module parses YAML only far enough to know where a
block begins and ends; it never re-serializes. A round trip through a YAML
loader is lossy in exactly the direction that matters here: comments. In the
registries this feature exists for, the comments are where the reasoning
lives — which branch is the running one, which board is closed, why a path is
not the obvious one. A reader that answers `--get` with clean re-serialized
YAML answers the letter of the question and drops the half that stops a wrong
action.

THE CARD NAMES EVERY TOP-LEVEL KEY. Kept whole, expanded into an index, or
merely named — but never absent. An index that silently omits a key is the
same failure as a budget that silently omits a file, one layer down: the thing
does not look missing, it looks like it was never there.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a documented dependency
    yaml = None

DEFAULT_LABEL = ("description", "display_name", "name")
DEFAULT_LABEL_CHARS = 120
CLI = "python3 scripts/context-index.py"

# A top-level key: column zero, not a comment, not a document marker.
TOP_KEY = re.compile(r"^([^\s#][^:]*):(?:\s|$)")


# ------------------------------------------------------------- structure --


def _is_blank(line: str) -> bool:
    return not line.strip()


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _child_key(line: str, indent: int):
    """The child name on `line` at exactly `indent`, or None."""
    if len(line) <= indent or line[:indent].strip() or _is_comment(line):
        return None
    if line[indent : indent + 1].isspace():
        return None
    match = re.match(r"^([^\s#][^:]*):(?:\s|$)", line[indent:])
    return match.group(1).strip() if match else None


def _trim(lines: list[str], start: int, end: int) -> int:
    """Drop the tail that belongs to whatever comes NEXT, not to this block.

    Order matters. Comment lines are dropped FIRST, and only while they run
    unbroken to the next key: those introduce the next key, and a block that
    keeps them files its neighbour's reasoning under its own name. A comment
    followed by a blank line is separated from the next key, so it stays with
    this block. Then the blank lines go.
    """
    while end > start + 1 and _is_comment(lines[end - 1]):
        end -= 1
    while end > start + 1 and _is_blank(lines[end - 1]):
        end -= 1
    return end


def parse_source(text: str) -> dict:
    """Top-level blocks in file order, with line spans and child spans.

    Returns `{name: {"kind", "start", "end", "children": {name: (start, end)}}}`
    where spans are half-open line indices into `text.split("\\n")`.
    """
    lines = text.split("\n")
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        match = TOP_KEY.match(line)
        if match:
            starts.append((i, match.group(1).strip()))

    blocks: dict[str, dict] = {}
    for pos, (start, name) in enumerate(starts):
        end = _trim(lines, start, starts[pos + 1][0] if pos + 1 < len(starts) else len(lines))

        inline = lines[start].split(":", 1)[1].strip()
        body = [
            line
            for line in lines[start + 1 : end]
            if not _is_blank(line) and not _is_comment(line)
        ]
        if inline and not inline.startswith("#"):
            kind = "scalar"
        elif body and body[0].lstrip().startswith("- "):
            kind = "list"
        elif body:
            kind = "map"
        else:
            kind = "scalar"

        children: dict[str, tuple[int, int]] = {}
        if kind == "map":
            indent = len(body[0]) - len(body[0].lstrip())
            marks: list[tuple[int, str]] = []
            for i in range(start + 1, end):
                child = _child_key(lines[i], indent)
                if child:
                    marks.append((i, child))
            for idx, (cstart, cname) in enumerate(marks):
                cend = _trim(
                    lines, cstart, marks[idx + 1][0] if idx + 1 < len(marks) else end
                )
                children[cname] = (cstart, cend)

        blocks[name] = {
            "kind": kind,
            "start": start,
            "end": end,
            "children": children,
        }
    return blocks


def _with_leading_comments(lines: list[str], start: int) -> int:
    """Walk back over comment lines that TOUCH `start` (no blank between).

    Contiguity is the whole rule. A comment separated by a blank line belongs
    to the section, not to the entry below it, and dragging it along would
    attach one entry's reasoning to its neighbour.
    """
    i = start
    while i > 0 and _is_comment(lines[i - 1]) and not _is_blank(lines[i - 1]):
        i -= 1
    return i


def slice_block(text: str, dotted: str) -> str:
    """The raw text of `dotted` (`section` or `section.entry`), comments kept."""
    lines = text.split("\n")
    blocks = parse_source(text)
    head, _, tail = dotted.partition(".")
    if head not in blocks:
        return ""
    block = blocks[head]
    if not tail:
        start, end = block["start"], block["end"]
    else:
        if tail not in block["children"]:
            return ""
        start, end = block["children"][tail]
    start = _with_leading_comments(lines, start)
    return "\n".join(lines[start:end]).rstrip() + "\n"


# ------------------------------------------------------------ the card --


def resolve_card(text: str, card) -> dict:
    """The effective card: the declaration, or one detected from the shape.

    FAIL-OPEN. An instance that never declares anything still gets a usable
    index, and therefore never has to choose between adopting this feature and
    keeping its registry readable.
    """
    blocks = parse_source(text)
    if card:
        resolved = dict(card)
        resolved.setdefault("keep", [])
        resolved.setdefault("sections", [])
    else:
        lines = text.split("\n")
        keep, sections = [], []
        for name, block in blocks.items():
            children = block["children"]
            # ALL children, not any. A map whose children are mixed is a block
            # of settings, and settings are what a session needs in front of
            # it — `work.enabled` decides whether Phase 1 runs at all. When the
            # shape is ambiguous, auto-detection carries more, never less.
            if (
                block["kind"] == "map"
                and children
                and all(_looks_nested(lines, span) for span in children.values())
            ):
                sections.append(name)
            elif block["kind"] in ("scalar", "map"):
                keep.append(name)
        resolved = {"kind": "index", "keep": keep, "sections": sections}
    resolved.setdefault("label", DEFAULT_LABEL)
    resolved.setdefault("label_chars", DEFAULT_LABEL_CHARS)
    return resolved


def _looks_nested(lines: list[str], span: tuple[int, int]) -> bool:
    """True when the child at `span` is a block rather than a scalar.

    Reads the span, never a rendered slice: a slice carries the entry's
    leading comment as its first line, and reading a comment as the key line
    answers this question at random.
    """
    start, end = span
    key = lines[start]
    inline = key.split(":", 1)[1].strip() if ":" in key else ""
    if inline and not inline.startswith("#"):
        return False
    return any(
        not _is_blank(line) and not _is_comment(line)
        for line in lines[start + 1 : end]
    )


def addressable(text: str, card=None) -> list[str]:
    """Every path `--get` accepts, in file order."""
    resolved = resolve_card(text, card)
    blocks = parse_source(text)
    paths: list[str] = []
    for name, block in blocks.items():
        paths.append(name)
        if name in resolved["sections"]:
            paths.extend(f"{name}.{child}" for child in block["children"])
    return paths


def _label_for(text: str, section: str, child: str, field, chars: int) -> str:
    """The one line for an entry, from the first declared field that has one.

    `label` may name several fields, tried in order. Entry families disagree
    about what their one line is called — repos say `description`, customers
    say `display_name` — and an index whose lines are empty is just a list of
    names.
    """
    fields = [field] if isinstance(field, str) else list(field or [])
    block = slice_block(text, f"{section}.{child}")
    value = None
    if yaml is not None:
        try:
            parsed = yaml.safe_load(block) or {}
            entry = parsed.get(child) if isinstance(parsed, dict) else None
            if isinstance(entry, dict):
                for name in fields:
                    if isinstance(entry.get(name), str) and entry[name].strip():
                        value = entry[name]
                        break
        except yaml.YAMLError:
            value = None
    if not isinstance(value, str) or not value.strip():
        return ""
    flat = " ".join(value.split())
    return flat if len(flat) <= chars else flat[: chars - 1].rstrip() + "…"


def uncarried_comment_bytes(text: str, card=None) -> int:
    """Comment bytes inside indexed sections, i.e. not resident in the card.

    They remain reachable through `--get`; they stop being in front of the
    reader by default. That distinction is the real cost of this feature, and
    a migration that does not see the number will discover it the hard way:
    a guardrail written as a YAML comment stops guarding the day the file
    stops being read whole.
    """
    resolved = resolve_card(text, card)
    lines = text.split("\n")
    blocks = parse_source(text)
    total = 0
    for name in resolved["sections"]:
        block = blocks.get(name)
        if not block:
            continue
        for line in lines[block["start"] : block["end"]]:
            if _is_comment(line):
                total += len(line.encode("utf-8")) + 1
    return total


def render_card(text: str, card, rel_path: str) -> str:
    """The always-on surface for `rel_path`. Deterministic by construction."""
    resolved = resolve_card(text, card)
    blocks = parse_source(text)
    kept = [n for n in blocks if n in resolved["keep"]]
    sections = [n for n in blocks if n in resolved["sections"]]
    rest = [n for n in blocks if n not in kept and n not in sections]

    out = [f"# {rel_path} — index", ""]

    if kept:
        out.append("Kept whole:")
        out.append("")
        for name in kept:
            out.append(slice_block(text, name).rstrip())
            out.append("")

    entries = sum(len(blocks[n]["children"]) for n in sections)
    out.append(
        f"{len(sections)} section(s), {entries} entr{'y' if entries == 1 else 'ies'}. "
        f"Read one with `{CLI} {rel_path} --get <path>`."
    )
    out.append("")

    for name in sections:
        out.append(f"## {name}")
        for child in blocks[name]["children"]:
            label = _label_for(
                text, name, child, resolved["label"], int(resolved["label_chars"])
            )
            out.append(f"- **{child}**" + (f" — {label}" if label else " — (no label)"))
        out.append("")

    if rest:
        described = []
        for name in rest:
            block = blocks[name]
            if block["kind"] == "list":
                count = sum(
                    1
                    for line in text.split("\n")[block["start"] + 1 : block["end"]]
                    if line.lstrip().startswith("- ")
                )
                described.append(f"{name} (list of {count})")
            else:
                described.append(name)
        out.append("Also present, fetch by name: " + ", ".join(described) + ".")
        out.append("")

    lost = uncarried_comment_bytes(text, resolved)
    if lost:
        out.append(
            f"{lost} bytes of comment sit inside the indexed sections and are not "
            f"resident here; `--get` still returns them with their entry."
        )
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- guards --


def check_round_trip(text: str, card=None, extra_paths=None) -> list[str]:
    """Every advertised path has to slice to something.

    A pointer reads as a promise that the content is one call away, and the
    caller stops looking anywhere else. A dead one is worse than no index.
    """
    paths = list(addressable(text, card)) + list(extra_paths or [])
    return [
        f"{path}: advertised in the index and slices to nothing"
        for path in paths
        if not slice_block(text, path).strip()
    ]


def check_declaration(text: str, card=None) -> list[str]:
    """Names in `keep:` / `sections:` that the source does not have.

    The declaration is the failure surface this feature adds, and a typo in it
    is absorbed in silence: the real key falls through to "also present", its
    content stops being resident, and the card still renders and still passes
    its cap. Auto-detection cannot make this mistake, so an undeclared source
    has nothing to check.
    """
    if not card:
        return []
    present = set(parse_source(text))
    findings = []
    for field in ("keep", "sections"):
        for name in card.get(field) or []:
            if name not in present:
                findings.append(
                    f"{field}: '{name}' is declared and not in the source"
                )
    return findings


def declared_cards(repo_root) -> dict:
    """Every `path -> card` declared in the budget, user overlay last."""
    if yaml is None:  # pragma: no cover
        return {}
    root = Path(repo_root)
    cards: dict[str, dict] = {}
    for name in ("context-budget.yaml", "context-budget.user.yaml"):
        path = root / name
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for rel, item in (data.get("items") or {}).items():
            if (item or {}).get("card"):
                cards[rel] = item["card"]
    return cards


def check_coverage(text: str, rendered: str = "") -> list[str]:
    """Every top-level key has to be findable in the card, in some form."""
    return [
        f"{name}: present in the source and absent from the card"
        for name in parse_source(text)
        if name not in rendered
    ]


# ----------------------------------------------------------- declaration --


def card_for(repo_root, rel_path: str):
    """The `card:` block declared for `rel_path`, or None.

    One declaration file, read by both halves: the meter measures the card the
    session will really get, and the session gets the card the meter measured.
    Two sources of truth here would mean a green gate over a different card.
    """
    if yaml is None:  # pragma: no cover
        return None
    root = Path(repo_root)
    card = None
    for name in ("context-budget.yaml", "context-budget.user.yaml"):
        path = root / name
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        item = (data.get("items") or {}).get(rel_path) or {}
        if item.get("card"):
            card = item["card"]
    return card
