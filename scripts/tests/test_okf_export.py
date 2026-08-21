#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/okf-export.py (the Tier 1 OKF v0.2 exporter).

CONTRACT — this file is the authoritative spec for the exporter's public
surface (read against scripts/extract-frontmatter.py and
scripts/gen-board.py for the existing hand-rolled-parsing conventions this
repo already uses). scripts/okf-export.py implements this exact surface:

    OKF_VERSION: str                                            = "0.2"
    EXPORTER_VERSION: str                                       = "1.0"
        The exporter's OWN version, deliberately a separate constant from
        the spec version so the two can never drift into each other.

    default_generated_by() -> str
        f"okf-export/{EXPORTER_VERSION}". Names the transformation that
        produced the bundle document, NEVER the author of the underlying
        knowledge. Must not read git, $USER, or any environment.

    normalize_timestamp(value: str) -> str | None
        Two accepted shapes, everything else -> None:
        a full `YYYY-MM-DD` calendar date -> widened to `<date>T00:00:00Z`;
        an ISO datetime that already carries an explicit offset -> verbatim.
        None for a partial date, the literal `YYYY-MM-DD` placeholder, a
        calendar-impossible date, a naive datetime, free text and "". The
        strict regex gate runs BEFORE date.fromisoformat, which on Python
        3.11+ would otherwise accept `20260702` and `2026-W27-1`. Pure:
        reads only its argument, never the clock.

    parse_frontmatter(text: str) -> tuple[dict, str]
        Hand-rolled (NO PyYAML dependency, mirrors gen-board.py's
        parse_status()). Skips leading `# ...` comment lines (the
        `# yaml-language-server: $schema=...` prolog convention) before the
        first `---` fence. Reads the first `---`...`---` block as flat
        `key: value` scalar pairs, stripping a trailing inline `# comment`
        and surrounding quotes. Returns (frontmatter_dict, body_text).
        A file with NO frontmatter block returns ({}, text) — the body is
        the untouched original text.

    concept_slug(path: Path) -> str
        `STATUS.md` -> parent directory name (task/stream slug, same
        convention as gen-board.py). Any other filename -> the file's stem.

    derive_title(frontmatter: dict, body: str, fallback: str) -> str
        frontmatter["title"] -> first `# ` H1 line in body -> fallback.

    derive_description(frontmatter: dict, body: str) -> str
        frontmatter["description"] -> frontmatter["summary"] ->
        frontmatter["headline"] -> "" (never derived from body).

    resolve_wikilinks(text: str, slug_to_relpath: dict[str, str]) -> tuple[str, list[str]]
        Replaces every `[[slug]]` occurrence where slug is a kebab-case
        identifier (`[a-z][a-z0-9-]*`): if slug is a key in
        slug_to_relpath, becomes a markdown link `[slug](relpath)`;
        otherwise the `[[slug]]` text is left completely untouched (OKF
        tolerates dangling references; rewriting them would corrupt
        content) and the slug is appended to the returned unresolved list.
        Non-kebab bracket pairs — e.g. bash `[[ -f file ]]` conditionals
        inside code blocks — never match and are never reported.

    discover_sources(root: Path, scope: str) -> list[Path]
        scope == "user": every work/tasks/*/STATUS.md,
        work/streams/*/STATUS.md, work/done/*/*/STATUS.md, every
        */deliverables/*.md under work/, every docs/**/*.md, rules/**/*.md
        and examples/**/*.md under root.
        scope == "core": ONLY docs/**/*.md + examples/**/*.md (work/ and
        rules/ excluded entirely — this is the public-safe subset for a
        demo export).
        Any other scope string raises ValueError.

    concept_type_for(path: Path, root: Path) -> str
        .../work/tasks/<slug>/STATUS.md      -> "task"
        .../work/streams/<slug>/STATUS.md    -> "stream"
        .../work/done/<month>/<slug>/STATUS.md -> "task"
        .../deliverables/*.md                -> "deliverable"
        docs/**/*.md                         -> "doc"
        rules/**/*.md                        -> "rule"
        examples/**/*.md                     -> "example"

    build_concept(path: Path, root: Path) -> dict
        {"slug": str, "okf_type": str, "title": str, "description": str,
         "resource": str, "generated_at": str | None,
         "bridge_status": str | None, "tags": list[str], "body": str}
        `generated_at` <- normalize_timestamp(frontmatter["last_updated"] or
        frontmatter["created"] or ""). `bridge_status` <-
        frontmatter["status"] or None — a Bridge WORKFLOW state, never OKF's
        own `status`. `tags` <- [frontmatter["status"]] +
        [frontmatter["context"]] (only the ones present), else [].
        `body` is the RAW markdown body (wikilinks not yet resolved).

    write_bundle(root, out_dir, scope, memory_dir=None, generated_by=None) -> dict
        Orchestrates discover_sources -> build_concept (all) -> a
        slug->relpath index -> resolve_wikilinks over every body -> writes
        out_dir/<type>/<slug>.md for every populated type, writes
        out_dir/<type>/index.md per populated type directory, and writes
        out_dir/index.md (root) whose frontmatter carries
        `okf_version: "0.2"` AND NOTHING ELSE (the one key OKF permits in an
        index file). `generated_by` is run-wide, defaulting to
        default_generated_by(). Deterministic + idempotent: re-running with
        unchanged input produces BYTE-IDENTICAL files. Returns a manifest:
        {"okf_version": "0.2", "scope": scope, "concept_count": int,
        "generated_by": str, "concepts_without_generated_at": int,
        "unresolved_wikilinks": list[str]} — the undated figure is a COUNT,
        never a list of source paths, because the manifest is printed.

    Emitted concept frontmatter, in order, empty fields OMITTED:
        type, title, description?, resource, generated { by, at? },
        bridge_status?, tags?

    NEVER EMITTED, and each an explicit decision rather than an oversight:
        timestamp    superseded by generated.at in v0.2; not dual-emitted,
                     because the spec's legacy fallback applies only when
                     `generated` is absent, which it never is.
        status       OKF `status` is document readiness; a Bridge status is
                     work state, and `draft` is a homograph across the two.
        verified     nothing in a Bridge instance is a verification event,
                     and trust tiers are derived purely from this field.
        sources      `related:` is see-also, not derived-from; a fabricated
                     entry manufactures a lineage edge consumers walk.
        stale_after  a consumer GATE; no Bridge field says when a document
                     stops being true. If ever adopted it is an absolute ISO
                     instant with an offset, never a bare date, never a TTL.

    main(argv: list[str] | None = None) -> int
        argparse CLI: --root (default "."), --out (required), --scope
        {"user","core"} (default "user"; invalid choice -> argparse
        SystemExit), --memory-dir, --generated-by ACTOR. A --generated-by
        value that is not an OKF actor (`<producer>/<version>`, `human:<id>`
        or `process:<id>`) -> stderr + exit 1, no bundle written. A `human:`
        actor with --scope core -> stderr WARNING, still exits 0.
        Non-existent/non-dir --root -> prints to stderr, returns a non-zero
        exit code (no traceback). On success prints a one-line summary and
        returns 0.

Hermetic: every test builds its own synthetic mini-instance under tmp_path
(generic names only — "acme", "sample-task" — never real repo/customer
content) and never touches the real repo tree beyond importing the module
under test.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tokenize
import types
from pathlib import Path

import pytest
import yaml  # TEST-only dependency — never imported by the exporter itself

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "okf-export.py"


def _pyyaml_frontmatter(path: Path) -> dict:
    """Parse a bundle file's frontmatter block with a REAL YAML parser.

    Deliberately NOT parse_frontmatter: validating a lenient producer with
    its own lenient reader is the same procedure run twice, and lets a
    malformed block round-trip "successfully". A real parser is what an
    actual OKF consumer will use, so it is what conformance must be
    measured against.
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} carries no frontmatter fence"
    _, block, _ = text.split("---\n", 2)
    return yaml.safe_load(block) or {}


@pytest.fixture(scope="module")
def okf_export() -> types.ModuleType:
    """Load scripts/okf-export.py via importlib.util (not on sys.path)."""
    spec = importlib.util.spec_from_file_location("okf_export", MODULE_PATH)
    assert spec is not None and spec.loader is not None, (
        f"could not build an import spec for {MODULE_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["okf_export"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def bridge_root(tmp_path: Path) -> Path:
    """A synthetic mini Bridge instance — generic acme/sample-* names only."""
    root = tmp_path / "acme-instance"

    _write(
        root / "work/tasks/sample-task/STATUS.md",
        "---\n"
        "type: task\n"
        "status: doing\n"
        "context: acme\n"
        "priority: P2\n"
        "created: 2026-01-01\n"
        "last_updated: 2026-01-05\n"
        'headline: "Kickoff automation for Acme"\n'
        "---\n\n"
        "# Sample Task\n\n"
        "Body text describing the sample task for the fixture.\n",
    )
    _write(
        root / "work/tasks/sample-task/deliverables/summary.md",
        "---\n"
        'summary: "Deliverable summary text for Acme"\n'
        "last_updated: 2026-01-04\n"
        "---\n\n"
        "# Summary\n\n"
        "Deliverable body content.\n",
    )
    _write(
        root / "work/streams/sample-stream/STATUS.md",
        "---\n"
        "type: stream\n"
        "status: doing\n"
        "context: acme\n"
        "created: 2026-01-01\n"
        "---\n\n"
        "# Sample Stream\n\n"
        "Stream body content.\n",
    )
    _write(
        root / "work/done/2026-01/finished-task/STATUS.md",
        "---\n"
        "status: done\n"
        "outcome: shipped\n"
        "context: acme\n"
        "created: 2025-12-01\n"
        "last_updated: 2026-01-02\n"
        "---\n\n"
        "# Finished Task\n\n"
        "Closed task body.\n",
    )
    _write(
        root / "docs/sample-doc.md",
        "# yaml-language-server: $schema=./_schema.yaml\n"
        "---\n"
        'summary: "Doc about acme"\n'
        "last_updated: 2026-01-03\n"
        "---\n\n"
        "# Sample Doc\n\n"
        "See [[sample-rule]] and [[missing-thing]] for more.\n",
    )
    _write(
        root / "rules/sample-rule.md",
        "# Sample Rule\n\n"
        "Some rule text with no frontmatter at all.\n",
    )
    _write(
        root / "examples/acme-demo/README.md",
        "# Acme Demo\n\n"
        "Example content for the acme demo.\n",
    )
    return root


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """A synthetic auto-memory directory (lives OUTSIDE the instance root)."""
    mem = tmp_path / "memory"

    _write(
        mem / "feedback_acme_preference.md",
        "---\n"
        "name: acme-preference\n"
        "description: Acme prefers weekly summaries over daily pings\n"
        "metadata:\n"
        "  type: feedback\n"
        "---\n\n"
        "Acme prefers weekly summaries. See [[sample-rule]].\n",
    )
    _write(
        mem / "reference_acme_endpoint.md",
        "---\n"
        "name: acme-endpoint\n"
        "description: The Acme staging endpoint and its auth quirk\n"
        "metadata:\n"
        "  type: reference\n"
        "---\n\n"
        "Staging lives behind the acme gateway.\n",
    )
    # Index + provenance files and frontmatter-less strays must be skipped.
    _write(mem / "MEMORY.md", "# Memory Index\n\n- [Acme preference](feedback_acme_preference.md)\n")
    _write(mem / "MEMORY-ARCHIVE.md", "# Archive\n")
    _write(mem / "PROVENANCE.md", "# Provenance\n")
    _write(mem / "scratch-note.md", "No frontmatter here, not a memory fact.\n")
    return mem


# --------------------------------------------------------------------------
# parse_frontmatter
# --------------------------------------------------------------------------

def test_parse_frontmatter_extracts_scalar_fields(okf_export):
    text = "---\nstatus: doing\ncontext: acme\n---\n\nBody paragraph.\n"
    fm, body = okf_export.parse_frontmatter(text)
    assert fm == {"status": "doing", "context": "acme"}
    assert body.strip() == "Body paragraph."


def test_parse_frontmatter_skips_yaml_language_server_comment_prolog(okf_export):
    text = (
        "# yaml-language-server: $schema=./_schema.yaml\n"
        "---\nstatus: doing\n---\n\nBody.\n"
    )
    fm, body = okf_export.parse_frontmatter(text)
    assert fm.get("status") == "doing"
    assert body.strip() == "Body."


def test_parse_frontmatter_strips_quotes_and_inline_comments(okf_export):
    text = (
        '---\nheadline: "Kickoff for Acme"  # short desc\ncontext: acme\n---\n\nBody.\n'
    )
    fm, _ = okf_export.parse_frontmatter(text)
    assert fm.get("headline") == "Kickoff for Acme"
    assert fm.get("context") == "acme"


def test_parse_frontmatter_no_block_returns_empty_dict_and_full_body(okf_export):
    text = "# Just A Heading\n\nNo frontmatter block at all.\n"
    fm, body = okf_export.parse_frontmatter(text)
    assert fm == {}
    assert body == text


# --------------------------------------------------------------------------
# concept_slug
# --------------------------------------------------------------------------

def test_concept_slug_uses_parent_dir_for_status_md(okf_export, bridge_root):
    p = bridge_root / "work/tasks/sample-task/STATUS.md"
    assert okf_export.concept_slug(p) == "sample-task"


def test_concept_slug_uses_filename_stem_for_other_files(okf_export, bridge_root):
    assert okf_export.concept_slug(bridge_root / "docs/sample-doc.md") == "sample-doc"
    assert (
        okf_export.concept_slug(bridge_root / "work/tasks/sample-task/deliverables/summary.md")
        == "summary"
    )


# --------------------------------------------------------------------------
# derive_title / derive_description
# --------------------------------------------------------------------------

def test_derive_title_prefers_frontmatter_title_field(okf_export):
    fm = {"title": "Explicit Title"}
    body = "# Different H1\n\nBody."
    assert okf_export.derive_title(fm, body, fallback="fallback-slug") == "Explicit Title"


def test_derive_title_falls_back_to_h1_heading(okf_export):
    fm = {}
    body = "# Heading From Body\n\nBody text."
    assert okf_export.derive_title(fm, body, fallback="fallback-slug") == "Heading From Body"


def test_derive_title_falls_back_to_provided_fallback(okf_export):
    fm = {}
    body = "No heading here at all."
    assert okf_export.derive_title(fm, body, fallback="fallback-slug") == "fallback-slug"


def test_derive_description_prefers_description_then_summary_then_headline(okf_export):
    assert okf_export.derive_description(
        {"description": "D", "summary": "S", "headline": "H"}, ""
    ) == "D"
    assert okf_export.derive_description({"summary": "S", "headline": "H"}, "") == "S"
    assert okf_export.derive_description({"headline": "H"}, "") == "H"


def test_derive_description_empty_when_nothing_found(okf_export):
    assert okf_export.derive_description({}, "Body without any explicit fields.") == ""


# --------------------------------------------------------------------------
# resolve_wikilinks
# --------------------------------------------------------------------------

def test_resolve_wikilinks_replaces_resolved_slug_with_markdown_link(okf_export):
    text = "See [[sample-rule]] for details."
    new_text, unresolved = okf_export.resolve_wikilinks(text, {"sample-rule": "rule/sample-rule.md"})
    assert "[[sample-rule]]" not in new_text
    assert "[sample-rule](rule/sample-rule.md)" in new_text
    assert unresolved == []


def test_resolve_wikilinks_leaves_unresolved_slug_untouched_and_reports_it(okf_export):
    text = "See [[missing-thing]] for details."
    new_text, unresolved = okf_export.resolve_wikilinks(text, {})
    assert new_text == text  # dangling reference left verbatim, never rewritten
    assert unresolved == ["missing-thing"]


def test_resolve_wikilinks_ignores_bash_conditionals_and_non_kebab_brackets(okf_export):
    text = 'if [[ -f "$file" ]]; then\n  use [[Wiki Style]] links\nfi\n'
    new_text, unresolved = okf_export.resolve_wikilinks(text, {"file": "doc/file.md"})
    assert new_text == text  # neither bracket pair is a kebab wikilink
    assert unresolved == []


def test_resolve_wikilinks_no_wikilinks_returns_text_unchanged(okf_export):
    text = "Plain text, no links here."
    new_text, unresolved = okf_export.resolve_wikilinks(text, {"x": "y"})
    assert new_text == text
    assert unresolved == []


# --------------------------------------------------------------------------
# discover_sources
# --------------------------------------------------------------------------

def test_discover_sources_user_scope_finds_all_seven_fixture_files(okf_export, bridge_root):
    paths = okf_export.discover_sources(bridge_root, "user")
    rels = {p.relative_to(bridge_root).as_posix() for p in paths}
    assert rels == {
        "work/tasks/sample-task/STATUS.md",
        "work/tasks/sample-task/deliverables/summary.md",
        "work/streams/sample-stream/STATUS.md",
        "work/done/2026-01/finished-task/STATUS.md",
        "docs/sample-doc.md",
        "rules/sample-rule.md",
        "examples/acme-demo/README.md",
    }


def test_discover_sources_core_scope_excludes_work_and_rules(okf_export, bridge_root):
    paths = okf_export.discover_sources(bridge_root, "core")
    rels = {p.relative_to(bridge_root).as_posix() for p in paths}
    assert rels == {"docs/sample-doc.md", "examples/acme-demo/README.md"}


def test_discover_sources_unknown_scope_raises_value_error(okf_export, bridge_root):
    with pytest.raises(ValueError):
        okf_export.discover_sources(bridge_root, "bogus")


# --------------------------------------------------------------------------
# concept_type_for
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "relpath,expected_type",
    [
        ("work/tasks/sample-task/STATUS.md", "task"),
        ("work/streams/sample-stream/STATUS.md", "stream"),
        ("work/done/2026-01/finished-task/STATUS.md", "task"),
        ("work/tasks/sample-task/deliverables/summary.md", "deliverable"),
        ("docs/sample-doc.md", "doc"),
        ("rules/sample-rule.md", "rule"),
        ("examples/acme-demo/README.md", "example"),
    ],
)
def test_concept_type_for_maps_each_fixture_path_to_expected_type(
    okf_export, bridge_root, relpath, expected_type
):
    assert okf_export.concept_type_for(bridge_root / relpath, bridge_root) == expected_type


# --------------------------------------------------------------------------
# build_concept
# --------------------------------------------------------------------------

def test_build_concept_task_status_maps_headline_status_context_and_timestamp(okf_export, bridge_root):
    c = okf_export.build_concept(bridge_root / "work/tasks/sample-task/STATUS.md", bridge_root)
    assert c["slug"] == "sample-task"
    assert c["okf_type"] == "task"
    assert c["title"] == "Sample Task"
    assert c["description"] == "Kickoff automation for Acme"
    assert c["generated_at"] == "2026-01-05T00:00:00Z"
    assert c["bridge_status"] == "doing"
    assert "doing" in c["tags"] and "acme" in c["tags"]


def test_build_concept_doc_uses_summary_field_and_h1_title(okf_export, bridge_root):
    c = okf_export.build_concept(bridge_root / "docs/sample-doc.md", bridge_root)
    assert c["okf_type"] == "doc"
    assert c["title"] == "Sample Doc"
    assert c["description"] == "Doc about acme"
    assert c["generated_at"] == "2026-01-03T00:00:00Z"
    assert c["bridge_status"] is None
    assert c["tags"] == []


def test_build_concept_rule_without_frontmatter_falls_back_to_h1_and_empty_fields(okf_export, bridge_root):
    c = okf_export.build_concept(bridge_root / "rules/sample-rule.md", bridge_root)
    assert c["okf_type"] == "rule"
    assert c["title"] == "Sample Rule"
    assert c["description"] == ""
    assert c["generated_at"] is None
    assert c["tags"] == []


# --------------------------------------------------------------------------
# write_bundle
# --------------------------------------------------------------------------

def test_write_bundle_user_scope_manifest_counts_and_version(okf_export, bridge_root, tmp_path):
    out = tmp_path / "bundle-user"
    manifest = okf_export.write_bundle(bridge_root, out, "user")
    assert manifest["okf_version"] == "0.2"
    assert manifest["scope"] == "user"
    assert manifest["concept_count"] == 7
    assert "missing-thing" in manifest["unresolved_wikilinks"]


def test_write_bundle_root_index_declares_okf_version(okf_export, bridge_root, tmp_path):
    out = tmp_path / "bundle-root-index"
    okf_export.write_bundle(bridge_root, out, "user")
    fm, _ = okf_export.parse_frontmatter((out / "index.md").read_text(encoding="utf-8"))
    assert fm.get("okf_version") == "0.2"


def test_write_bundle_creates_per_type_index_files(okf_export, bridge_root, tmp_path):
    out = tmp_path / "bundle-type-index"
    okf_export.write_bundle(bridge_root, out, "user")
    for concept_type in ("task", "stream", "deliverable", "doc", "rule", "example"):
        assert (out / concept_type / "index.md").exists(), concept_type


def test_write_bundle_doc_concept_roundtrips_resolved_and_unresolved_wikilinks(okf_export, bridge_root, tmp_path):
    out = tmp_path / "bundle-doc-roundtrip"
    okf_export.write_bundle(bridge_root, out, "user")
    content = (out / "doc" / "sample-doc.md").read_text(encoding="utf-8")
    fm, body = okf_export.parse_frontmatter(content)
    assert fm.get("type") == "doc"
    assert fm.get("title") == "Sample Doc"
    assert fm.get("description") == "Doc about acme"
    assert fm.get("resource") == "docs/sample-doc.md"
    assert "[[sample-rule]]" not in body
    assert "[[missing-thing]]" in body  # unresolved -> left verbatim
    assert "(/rule/sample-rule.md)" in body  # resolved -> bundle-root-relative


def test_write_bundle_core_scope_excludes_task_stream_deliverable_rule_dirs(okf_export, bridge_root, tmp_path):
    out = tmp_path / "bundle-core"
    manifest = okf_export.write_bundle(bridge_root, out, "core")
    assert manifest["concept_count"] == 2
    for concept_type in ("task", "stream", "deliverable", "rule"):
        assert not (out / concept_type).exists()
    assert (out / "doc").exists()
    assert (out / "example").exists()


def test_write_bundle_user_scope_exports_memory_facts(okf_export, bridge_root, memory_dir, tmp_path):
    out = tmp_path / "bundle-memory"
    manifest = okf_export.write_bundle(bridge_root, out, "user", memory_dir=memory_dir)
    assert manifest["concept_count"] == 9  # 7 repo concepts + 2 memory facts
    content = (out / "memory" / "acme-preference.md").read_text(encoding="utf-8")
    fm, body = okf_export.parse_frontmatter(content)
    assert fm.get("type") == "memory"
    assert fm.get("description") == "Acme prefers weekly summaries over daily pings"
    assert fm.get("resource") == "memory/feedback_acme_preference.md"
    # Memory body wikilinks resolve against the full bundle slug index:
    assert "(/rule/sample-rule.md)" in body
    # Index/provenance/frontmatter-less files are never exported as concepts:
    for skipped in ("MEMORY.md", "MEMORY-ARCHIVE.md", "PROVENANCE.md", "scratch-note.md"):
        assert not (out / "memory" / skipped).exists()
    assert (out / "memory" / "index.md").exists()  # generated type index only


def test_write_bundle_core_scope_never_exports_memory(okf_export, bridge_root, memory_dir, tmp_path):
    out = tmp_path / "bundle-core-no-memory"
    manifest = okf_export.write_bundle(bridge_root, out, "core", memory_dir=memory_dir)
    assert manifest["concept_count"] == 2
    assert not (out / "memory").exists()


@pytest.mark.parametrize(
    "hostile_name,expected_slug",
    [
        ("../../escaped", "escape-probe"),      # climbs out of the bundle entirely
        ("sub/dir/nested", "escape-probe"),     # silently creates a subdirectory
        ("/absolute", "escape-probe"),          # anchors at the filesystem root
        (".hidden", "escape-probe"),            # leading dot is not a slug
        ("fine-name", "fine-name"),             # the ordinary case still wins
    ],
)
def test_memory_name_cannot_escape_the_output_directory(
    okf_export, bridge_root, tmp_path, hostile_name, expected_slug
):
    """A memory fact's `name:` is arbitrary frontmatter, not a safe path.

    It was previously used verbatim as the output filename, so a name
    containing `..` or `/` made the exporter write outside `--out` — up to and
    including over a file in the source tree it is supposed to only read.
    """
    mem = tmp_path / "hostile-memory"
    # Real memory files are named `<type>_<slug>.md`, and the fallback strips
    # that type prefix — so this filename yields the slug "escape-probe".
    _write(
        mem / "reference_escape_probe.md",
        f"---\nname: {hostile_name}\ndescription: Probe\n---\n\nBody.\n",
    )
    out = tmp_path / "nested" / "bundle-escape"
    okf_export.write_bundle(bridge_root, out, "user", memory_dir=mem)

    assert (out / "memory" / f"{expected_slug}.md").is_file()
    written = [p for p in out.rglob("*.md")]
    for path in written:
        assert out in path.resolve().parents, f"{path} escaped {out}"
    # nothing landed beside or above the bundle
    assert not list((tmp_path / "nested").glob("*.md"))
    assert not list(tmp_path.glob("*.md"))


def test_default_memory_dir_derives_encoded_path_under_home(okf_export, tmp_path):
    derived = okf_export.default_memory_dir(tmp_path / "acme-instance")
    encoded = str((tmp_path / "acme-instance").resolve()).replace("/", "-")
    assert derived == Path.home() / ".claude" / "projects" / encoded / "memory"
    assert encoded.startswith("-")  # leading slash of the abs path becomes a leading dash


def _bundle_digest(out: Path) -> dict:
    """{bundle-relative path: sha256 of the file's bytes} for a whole bundle."""
    return {
        p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in out.rglob("*")
        if p.is_file()
    }


def test_write_bundle_is_byte_identical_on_rerun(okf_export, bridge_root, tmp_path):
    """Determinism is a load-bearing contract, so compare CONTENT, not names.

    The former path-only comparison passed even if every byte of every file
    had changed between runs — it only proved the same filenames were
    written, which is not what the contract promises.
    """
    out = tmp_path / "bundle-idempotent"
    manifest_1 = okf_export.write_bundle(bridge_root, out, "user")
    digest_1 = _bundle_digest(out)
    manifest_2 = okf_export.write_bundle(bridge_root, out, "user")
    digest_2 = _bundle_digest(out)
    assert manifest_1 == manifest_2
    assert digest_1 == digest_2


def test_module_source_has_no_wall_clock_call():
    """No render path may read the clock, a file mtime, or git.

    Any of these makes a re-export differ from the previous one even when
    the source tree is unchanged, breaking the byte-identical guarantee
    above. Asserted against the module source so the regression is caught at
    the exact line someone would write it.
    """
    # Comments and string literals are stripped first: a substring scan over
    # raw text would fire on the module's own comment explaining why git is
    # refused, which is documentation, not a call.
    tokens = tokenize.generate_tokens(io.StringIO(MODULE_PATH.read_text(encoding="utf-8")).readline)
    code = "".join(
        tok.string for tok in tokens if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER)
    )
    for forbidden in (
        "datetime.now", "utcnow", "date.today", "time.time", "st_mtime", "getmtime",
        # Not just the clock: git state and the environment are equally
        # non-reproducible, and a git-derived generated.by/at is exactly the
        # convenience a future contributor would reach for.
        "subprocess", "os.environ", "getenv", "popen", "check_output",
    ):
        assert forbidden not in code, f"non-reproducible call {forbidden!r} in the exporter"


def test_exporter_imports_without_pyyaml(bridge_root, tmp_path, monkeypatch):
    """The exporter is dependency-free; PyYAML is a TEST-only dependency.

    CI pre-installs PyYAML for this suite, so a stray ``import yaml`` in the
    exporter would pass CI and only fail for a user running the script on a
    bare interpreter. Blocking the module makes the constraint enforceable.
    """

    class _BlockYAML:
        def find_spec(self, fullname, path=None, target=None):
            del path, target  # part of the finder protocol, unused here
            if fullname == "yaml" or fullname.startswith("yaml."):
                raise ImportError("PyYAML is deliberately unavailable to the exporter")
            return None

    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockYAML(), *sys.meta_path])

    spec = importlib.util.spec_from_file_location("okf_export_no_yaml", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = module.write_bundle(bridge_root, tmp_path / "bundle-no-yaml", "user")
    assert manifest["concept_count"] == 7


# --------------------------------------------------------------------------
# OKF v0.2 — normalize_timestamp
# --------------------------------------------------------------------------

def test_normalize_timestamp_widens_bare_date_to_midnight_utc(okf_export):
    assert okf_export.normalize_timestamp("2026-07-02") == "2026-07-02T00:00:00Z"


@pytest.mark.parametrize(
    "value",
    ["2026-08-04T15:50:53Z", "2026-08-04T15:50:53+02:00", "2026-08-04T15:50:53-05:00"],
)
def test_normalize_timestamp_passes_through_offset_bearing_datetime(okf_export, value):
    """An instant that already carries an explicit offset is never rewritten."""
    assert okf_export.normalize_timestamp(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "YYYY-MM-DD",              # the literal placeholder work/templates/STATUS.md seeds
        "2026-03",                 # partial: would require inventing one day out of 31
        "2026-02-30",              # calendar-impossible
        "2026-08-04T15:50:53",     # naive: no offset, so not a knowable instant
        "sometime in March",
        "",
    ],
)
def test_normalize_timestamp_returns_none_for_unprovable_values(okf_export, value):
    """Refuse rather than guess — the field is simply omitted downstream."""
    assert okf_export.normalize_timestamp(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-30T00:00:00Z",       # calendar-impossible, offset-bearing
        "2026-06-31T09:00:00Z",       # June has 30 days
        "2026-13-45T99:99:99Z",       # every component out of range
        "2026-07-02T25:00:00Z",       # hour 25
        "2026-07-02T14:00:00+99:99",  # impossible offset
        "2026-07-02 14:00:00Z",       # space separator is not the ISO form
    ],
)
def test_normalize_timestamp_rejects_impossible_offset_instants(okf_export, value):
    """The offset branch must PROVE the value, not merely shape-match it.

    The regex is all `\\d{2}` groups, so month 13 and day-31-in-June match the
    pattern. Passing such a value through emits an unquoted scalar that no
    YAML parser can load, which breaks conformance for the entire bundle
    while the exporter still exits 0 and reports every date as proven.
    """
    assert okf_export.normalize_timestamp(value) is None


def test_impossible_source_instant_never_reaches_the_bundle(okf_export, tmp_path):
    """End-to-end companion to the unit test above."""
    root = tmp_path / "impossible-instant"
    _write(root / "docs/bad.md", "---\nlast_updated: 2026-06-31T09:00:00Z\n---\n\n# Bad\n\nBody.\n")
    out = tmp_path / "bundle-impossible"
    manifest = okf_export.write_bundle(root, out, "core")
    fm = _pyyaml_frontmatter(out / "doc" / "bad.md")   # must not raise
    assert "at" not in fm["generated"]
    assert manifest["concepts_without_generated_at"] == 1


@pytest.mark.parametrize("value", ["20260702", "2026-W27-1"])
def test_normalize_timestamp_rejects_compact_and_week_forms(okf_export, value):
    """The strict regex gate must run BEFORE date.fromisoformat.

    Python 3.11+ accepts both of these, so without the gate they would be
    silently widened into an instant the source never stated.
    """
    assert okf_export.normalize_timestamp(value) is None


# --------------------------------------------------------------------------
# OKF v0.2 — generated / trust / lifecycle fields
# --------------------------------------------------------------------------

CORE_SCOPE_KEY_ALLOWLIST = {
    "type", "title", "description", "resource", "generated", "bridge_status", "tags",
}


def _concept_files(out: Path) -> list[Path]:
    return [p for p in out.rglob("*.md") if p.name != "index.md"]


def _trust_tier(fm: dict) -> str:
    """OKF section 5.3's tier rule, implemented independently of the exporter."""
    verified = fm.get("verified")
    if not verified:
        return "unverified"
    entries = verified if isinstance(verified, list) else [verified]
    actors = [str(e.get("by", "")) for e in entries]
    return "human-reviewed" if any(a.startswith("human:") for a in actors) else "machine-confirmed"


def test_concept_emits_generated_flow_mapping_with_by(okf_export, bridge_root, memory_dir, tmp_path):
    """`by` is the only REQUIRED key inside `generated` (spec 5.2)."""
    out = tmp_path / "bundle-generated"
    okf_export.write_bundle(bridge_root, out, "user", memory_dir=memory_dir)
    for path in _concept_files(out):
        generated = _pyyaml_frontmatter(path).get("generated")
        assert isinstance(generated, dict), path
        assert generated.get("by") == f"okf-export/{okf_export.EXPORTER_VERSION}", path


def test_generated_at_present_for_dated_source_and_absent_for_undated(okf_export, bridge_root, tmp_path):
    out = tmp_path / "bundle-generated-at"
    okf_export.write_bundle(bridge_root, out, "user")
    # Asserted on the emitted TEXT: an unquoted ISO instant is what the spec's
    # own examples show, and PyYAML (YAML 1.1) resolves it to a native
    # datetime, so a value comparison would test the parser, not the output.
    assert "at: 2026-01-05T00:00:00Z" in (out / "task" / "sample-task.md").read_text(encoding="utf-8")
    assert "at" in _pyyaml_frontmatter(out / "task" / "sample-task.md")["generated"]
    undated = _pyyaml_frontmatter(out / "rule" / "sample-rule.md")["generated"]
    assert "at" not in undated


def test_generated_at_omitted_when_source_date_unparseable(okf_export, tmp_path):
    root = tmp_path / "partial-date-instance"
    _write(root / "docs/partial.md", "---\ncreated: 2026-03\n---\n\n# Partial\n\nBody.\n")
    out = tmp_path / "bundle-partial"
    okf_export.write_bundle(root, out, "core")
    fm = _pyyaml_frontmatter(out / "doc" / "partial.md")
    assert "at" not in fm["generated"]
    assert "2026-03" not in (out / "doc" / "partial.md").read_text(encoding="utf-8").split("---")[1]


def test_no_timestamp_key_emitted_for_any_concept(okf_export, bridge_root, memory_dir, tmp_path):
    """v0.2 clean break: `timestamp` is superseded by `generated.at`."""
    out = tmp_path / "bundle-no-timestamp"
    okf_export.write_bundle(bridge_root, out, "user", memory_dir=memory_dir)
    for path in _concept_files(out):
        assert "timestamp" not in _pyyaml_frontmatter(path), path


def test_no_concept_emits_okf_status_key(okf_export, bridge_root, memory_dir, tmp_path):
    """The homograph guard.

    OKF `status` is document readiness (draft|stable|deprecated); a Bridge
    `status` is WORK state (backlog|doing|review|done). Mapping one onto the
    other, even behind an enum whitelist, would turn a Bridge task in draft
    state into the OKF claim that the DOCUMENT is unreviewed and possibly
    incomplete. Absent `status` already means `stable`, which is the true
    claim about an exported write-up.
    """
    out = tmp_path / "bundle-no-status"
    okf_export.write_bundle(bridge_root, out, "user", memory_dir=memory_dir)
    for path in _concept_files(out):
        assert "status" not in _pyyaml_frontmatter(path), path


def test_bridge_status_is_namespaced_and_preserves_its_value(okf_export, hostile_root, tmp_path):
    out = tmp_path / "bundle-bridge-status"
    okf_export.write_bundle(hostile_root, out, "user")
    assert _pyyaml_frontmatter(out / "task" / "comma-tag.md")["bridge_status"] == "final, not sent"


def test_bridge_status_value_still_appears_in_tags(okf_export, bridge_root, tmp_path):
    """REGRESSION — the v0.1 tags derivation is untouched by the migration."""
    out = tmp_path / "bundle-tags-regression"
    okf_export.write_bundle(bridge_root, out, "user")
    tags = _pyyaml_frontmatter(out / "task" / "sample-task.md")["tags"]
    assert "doing" in tags and "acme" in tags


@pytest.mark.parametrize("field", ["verified", "sources", "stale_after", "usage_window"])
def test_no_concept_emits_a_fabricated_provenance_or_trust_field(okf_export, bridge_root, memory_dir, tmp_path, field):
    """Nothing in a Bridge instance honestly supplies any of these.

    `verified` is the costliest to fake: spec 5.3 derives trust tiers purely
    from it, so a synthetic entry silently promotes every concept in the
    bundle out of `unverified`. `stale_after` is a consumer GATE (10.5), so
    a fabricated horizon suppresses content that is perfectly current.
    """
    out = tmp_path / f"bundle-no-{field}"
    okf_export.write_bundle(bridge_root, out, "user", memory_dir=memory_dir)
    for path in _concept_files(out):
        assert field not in _pyyaml_frontmatter(path), path


def test_every_concept_derives_trust_tier_unverified(okf_export, bridge_root, memory_dir, tmp_path):
    """Asserts the consumer-visible consequence, not just key absence."""
    out = tmp_path / "bundle-trust-tier"
    okf_export.write_bundle(bridge_root, out, "user", memory_dir=memory_dir)
    for path in _concept_files(out):
        assert _trust_tier(_pyyaml_frontmatter(path)) == "unverified", path


def test_related_frontmatter_is_never_promoted_to_sources(okf_export, tmp_path):
    """`related:` is see-also, not derived-from (spec 5.1)."""
    root = tmp_path / "related-instance"
    _write(
        root / "docs/with-related.md",
        "---\nsummary: \"Doc with a related list\"\nrelated:\n  - other-doc.md\n  - ../scripts/x.py\n---\n\n# With Related\n\nBody.\n",
    )
    out = tmp_path / "bundle-related"
    okf_export.write_bundle(root, out, "core")
    fm = _pyyaml_frontmatter(out / "doc" / "with-related.md")
    assert "sources" not in fm
    assert "other-doc.md" not in str(fm)


def test_concept_with_no_frontmatter_invents_nothing(okf_export, bridge_root, tmp_path):
    """The anti-fabrication guard, stated as an exact key set."""
    out = tmp_path / "bundle-invents-nothing"
    okf_export.write_bundle(bridge_root, out, "user")
    fm = _pyyaml_frontmatter(out / "rule" / "sample-rule.md")
    assert set(fm) == {"type", "title", "resource", "generated"}
    assert fm["generated"] == {"by": f"okf-export/{okf_export.EXPORTER_VERSION}"}


def test_empty_description_and_tags_keys_are_omitted(okf_export, bridge_root, tmp_path):
    """Spec 5: absence carries meaning, so never write `description: ""`."""
    out = tmp_path / "bundle-omit-empty"
    okf_export.write_bundle(bridge_root, out, "user")
    fm = _pyyaml_frontmatter(out / "rule" / "sample-rule.md")
    assert "description" not in fm
    assert "tags" not in fm


# --------------------------------------------------------------------------
# OKF v0.2 — the --generated-by actor
# --------------------------------------------------------------------------

def test_generated_by_defaults_to_exporter_actor(okf_export):
    assert okf_export.EXPORTER_VERSION != okf_export.OKF_VERSION
    assert okf_export.default_generated_by() == f"okf-export/{okf_export.EXPORTER_VERSION}"


@pytest.mark.parametrize("actor", ["human:sample", "process:nightly-export", "some-tool/2.1"])
def test_generated_by_cli_override_used_verbatim(okf_export, bridge_root, tmp_path, actor):
    out = tmp_path / f"bundle-actor-{actor.replace(':', '-').replace('/', '-')}"
    rc = okf_export.main(
        ["--root", str(bridge_root), "--out", str(out), "--scope", "user", "--generated-by", actor]
    )
    assert rc == 0
    assert _pyyaml_frontmatter(out / "doc" / "sample-doc.md")["generated"]["by"] == actor


@pytest.mark.parametrize(
    "actor",
    [
        "Some Person", "human:", "no-slash-no-prefix", "a/b/c",
        # Python's `$` also matches BEFORE a trailing newline, so a `$`-anchored
        # gate would pass these and then split the rendered flow mapping across
        # two physical lines. The regex uses \Z for exactly this reason.
        "human:alice\n", "okf-export/1.0\n", "process:ci\n",
        'okf-export/1.0"', "okf-export/1.0 }",
    ],
)
def test_generated_by_rejects_non_actor_string_with_nonzero_exit(okf_export, bridge_root, tmp_path, actor):
    """A typo'd actor would silently mis-tier a whole bundle (spec 5.3/7)."""
    out = tmp_path / "bundle-bad-actor"
    rc = okf_export.main(
        ["--root", str(bridge_root), "--out", str(out), "--scope", "user", "--generated-by", actor]
    )
    assert rc == 1
    assert not out.exists()


def test_generated_by_never_reads_git_or_environment(okf_export, bridge_root, tmp_path, monkeypatch):
    """Locks the privacy + determinism boundary against a future 'convenience' default."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=bridge_root, check=True)
    subprocess.run(["git", "config", "user.name", "Real Person"], cwd=bridge_root, check=True)
    subprocess.run(["git", "config", "user.email", "real@example.com"], cwd=bridge_root, check=True)
    monkeypatch.setenv("USER", "realperson")

    out = tmp_path / "bundle-no-git-actor"
    okf_export.write_bundle(bridge_root, out, "user")
    rendered = (out / "doc" / "sample-doc.md").read_text(encoding="utf-8")
    assert f"okf-export/{okf_export.EXPORTER_VERSION}" in rendered
    assert "Real Person" not in rendered and "realperson" not in rendered


def test_core_scope_warns_when_generated_by_is_a_human_actor(okf_export, bridge_root, tmp_path, capsys):
    out = tmp_path / "bundle-core-human-actor"
    rc = okf_export.main(
        ["--root", str(bridge_root), "--out", str(out), "--scope", "core",
         "--generated-by", "human:sample"]
    )
    assert rc == 0
    assert "WARNING" in capsys.readouterr().err


def test_core_scope_frontmatter_keys_are_within_allowlist(okf_export, bridge_root, memory_dir, tmp_path):
    """Privacy regression for the newly added keys."""
    out = tmp_path / "bundle-core-allowlist"
    okf_export.write_bundle(bridge_root, out, "core", memory_dir=memory_dir)
    for path in _concept_files(out):
        fm = _pyyaml_frontmatter(path)
        assert set(fm) <= CORE_SCOPE_KEY_ALLOWLIST, (path, set(fm) - CORE_SCOPE_KEY_ALLOWLIST)
        blob = str(fm)
        assert "memory/" not in blob and "work/" not in blob and "human:" not in blob


# --------------------------------------------------------------------------
# OKF v0.2 — bundle level
# --------------------------------------------------------------------------

def test_root_index_frontmatter_carries_only_okf_version(okf_export, bridge_root, tmp_path):
    """Spec 8/12: `okf_version` is the ONE key permitted in an index.md."""
    out = tmp_path / "bundle-root-index-trim"
    okf_export.write_bundle(bridge_root, out, "user")
    index = out / "index.md"
    assert _pyyaml_frontmatter(index) == {"okf_version": "0.2"}
    body = index.read_text(encoding="utf-8")
    assert "scope: user" not in body.split("---")[1]
    assert "user" in body and "7" in body  # both still stated in the body prose


def test_is_bundle_dir_still_recognizes_a_v01_bundle(okf_export, tmp_path):
    """Trimming the root index must not make a v0.1 bundle unrecognisable.

    This asserts the predicate only. The guard it feeds is covered by the two
    BundleDestinationError tests below, which exercise the actual rmtree path.
    """
    old = tmp_path / "v01-bundle"
    old.mkdir()
    (old / "index.md").write_text('---\nokf_version: "0.1"\nscope: user\n---\n\n# OKF Bundle\n', encoding="utf-8")
    assert okf_export._is_bundle_dir(old) is True
    plain = tmp_path / "not-a-bundle"
    plain.mkdir()
    (plain / "index.md").write_text("# Just a readme, no frontmatter\n", encoding="utf-8")
    assert okf_export._is_bundle_dir(plain) is False


def test_write_bundle_refuses_to_clear_a_non_bundle_directory(okf_export, bridge_root, tmp_path):
    """The rmtree guard, exercised rather than assumed.

    `write_bundle` calls shutil.rmtree on --out. A non-empty directory that is
    not a prior bundle must raise BEFORE anything is deleted, or a mistyped
    --out silently destroys unrelated data.
    """
    precious = tmp_path / "precious"
    precious.mkdir()
    keeper = precious / "important.txt"
    keeper.write_text("do not delete me", encoding="utf-8")

    with pytest.raises(okf_export.BundleDestinationError):
        okf_export.write_bundle(bridge_root, precious, "user")
    assert keeper.exists(), "guard raised but the directory was cleared anyway"
    assert keeper.read_text(encoding="utf-8") == "do not delete me"


def test_write_bundle_refuses_an_out_dir_that_would_delete_the_source(okf_export, bridge_root, tmp_path):
    """--out == --root, or an ancestor of it, would clear the source tree."""
    del tmp_path
    for destination in (bridge_root, bridge_root.parent):
        with pytest.raises(okf_export.BundleDestinationError):
            okf_export.write_bundle(bridge_root, destination, "user")
    assert (bridge_root / "docs" / "sample-doc.md").exists()


@pytest.mark.parametrize("concept_type", ["task", "stream", "deliverable", "doc", "rule", "example", "memory"])
def test_type_index_has_no_frontmatter(okf_export, bridge_root, memory_dir, tmp_path, concept_type):
    out = tmp_path / "bundle-type-index-nofm"
    okf_export.write_bundle(bridge_root, out, "user", memory_dir=memory_dir)
    text = (out / concept_type / "index.md").read_text(encoding="utf-8")
    assert not text.startswith("---")


def test_type_index_entries_match_spec_bullet_form(okf_export, bridge_root, tmp_path):
    """Spec 8 shows `* [Title](relative-url) - short description`."""
    out = tmp_path / "bundle-type-index-form"
    okf_export.write_bundle(bridge_root, out, "user")
    entries = [
        line for line in (out / "doc" / "index.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("* [")
    ]
    assert entries, "no spec-form bullets found"
    assert "* [Sample Doc](sample-doc.md) - Doc about acme" in entries
    assert "—" not in (out / "doc" / "index.md").read_text(encoding="utf-8")


def test_no_concept_file_is_named_index_or_log(okf_export, tmp_path):
    """REGRESSION on _RESERVED_SLUGS + dedupe_slugs (spec 2 reserved names)."""
    root = tmp_path / "reserved-instance"
    _write(root / "docs/index.md", "---\nsummary: \"Would collide\"\n---\n\n# Index Doc\n\nBody.\n")
    _write(root / "docs/log.md", "---\nsummary: \"Would collide too\"\n---\n\n# Log Doc\n\nBody.\n")
    out = tmp_path / "bundle-reserved"
    okf_export.write_bundle(root, out, "core")
    assert (out / "doc" / "index-2.md").exists()
    assert (out / "doc" / "log-2.md").exists()
    assert not _pyyaml_frontmatter(out / "doc" / "index-2.md").get("okf_version")


def test_manifest_reports_generated_by_and_undated_count(okf_export, bridge_root, tmp_path):
    out = tmp_path / "bundle-manifest"
    manifest = okf_export.write_bundle(bridge_root, out, "user")
    assert manifest["okf_version"] == "0.2"
    assert manifest["generated_by"] == f"okf-export/{okf_export.EXPORTER_VERSION}"
    # sample-rule.md and examples/acme-demo/README.md carry no date at all
    assert manifest["concepts_without_generated_at"] == 2
    assert isinstance(manifest["concepts_without_generated_at"], int)


def test_contract_docstring_and_guide_declare_v0_2():
    """A half-migrated suite would freeze v0.1 semantics while the bundle says 0.2."""
    contract = Path(__file__).read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "okf-export.md").read_text(encoding="utf-8")
    assert 'OKF_VERSION: str' in contract and '"0.2"' in contract
    # The guide may REFERENCE v0.1 (the migration note does); what it must not
    # do is still describe itself as PRODUCING one.
    assert "(OKF) v0.2 bundle" in guide
    assert "(OKF) v0.1 bundle" not in guide
    for declined in ("verified", "sources", "stale_after"):
        assert declined in guide, f"guide does not state why {declined} is not emitted"


# --------------------------------------------------------------------------
# YAML safety — every emitted frontmatter block must survive a REAL parser
# --------------------------------------------------------------------------

@pytest.fixture
def hostile_root(tmp_path: Path) -> Path:
    """A mini instance whose frontmatter values are hostile to naive quoting.

    Kept separate from ``bridge_root`` on purpose: that fixture's file count
    is asserted verbatim by the discovery tests, so growing it would couple
    unrelated suites together.
    """
    root = tmp_path / "hostile-instance"
    _write(
        root / "docs/block-title.md",
        "---\n"
        "title: |\n"
        "  Line one\n"
        "  Line two\n"
        'summary: "Doc whose title is a block scalar"\n'
        "---\n\n"
        "Body.\n",
    )
    _write(
        root / "work/tasks/comma-tag/STATUS.md",
        "---\n"
        'status: "final, not sent"\n'
        "context: acme\n"
        "---\n\n"
        "# Comma Tag\n\nBody.\n",
    )
    _write(
        root / "work/tasks/bracket-tag/STATUS.md",
        "---\n"
        'status: "blocked [see issue 42]"\n'
        "context: acme\n"
        "---\n\n"
        "# Bracket Tag\n\nBody.\n",
    )
    # A colon-space is the block-context indicator, so unlike a comma or a
    # bracket it breaks `description:` and `bridge_status:` rather than the
    # flow sequence. Without it the quoting of those two positions is untested.
    _write(
        root / "docs/colon-desc.md",
        "---\n"
        'summary: "Note: this description carries a colon"\n'
        "---\n\n"
        "# Colon Desc\n\nBody.\n",
    )
    _write(
        root / "work/tasks/colon-status/STATUS.md",
        "---\n"
        'status: "blocked: waiting on review"\n'
        "---\n\n"
        "# Colon Status\n\nBody.\n",
    )
    return root


def test_colon_bearing_description_and_status_survive_a_real_parser(okf_export, hostile_root, tmp_path):
    """A colon-space would otherwise turn one scalar into a nested mapping."""
    out = tmp_path / "bundle-hostile-colon"
    okf_export.write_bundle(hostile_root, out, "user")
    assert _pyyaml_frontmatter(out / "doc" / "colon-desc.md")["description"] == (
        "Note: this description carries a colon"
    )
    assert _pyyaml_frontmatter(out / "task" / "colon-status.md")["bridge_status"] == (
        "blocked: waiting on review"
    )


def test_render_escapes_newlines_in_quoted_scalars(okf_export):
    """A multi-line scalar must render on ONE physical line, escaped.

    Today it is emitted verbatim, so the value silently folds (newline ->
    space) or, if a line happens to start with `---` or hold a control
    character, produces frontmatter no real parser can read at all.
    """
    quoted = okf_export._yaml_quote("Line one\nLine two")
    assert "\n" not in quoted
    assert quoted == '"Line one\\nLine two"'
    assert yaml.safe_load(f"title: {quoted}")["title"] == "Line one\nLine two"


@pytest.mark.parametrize(
    "value",
    [
        "Head\tTabbed\x01ctrl\rreturn",
        r"C:\path\to\thing",          # backslash: without escaping, YAML reads \t as a tab
        'He said "stop"',             # embedded quote closes the scalar
        r'mixed \ and " together',
        "trailing backslash \\",
    ],
)
def test_yaml_quote_roundtrips_through_a_real_parser(okf_export, value):
    quoted = okf_export._yaml_quote(value)
    assert yaml.safe_load(f"title: {quoted}")["title"] == value


def test_block_scalar_title_roundtrips_through_pyyaml(okf_export, hostile_root, tmp_path):
    """Both lines of a `title: |` block scalar survive the export."""
    out = tmp_path / "bundle-hostile-title"
    okf_export.write_bundle(hostile_root, out, "user")
    fm = _pyyaml_frontmatter(out / "doc" / "block-title.md")
    assert fm["title"] == "Line one\nLine two"


def test_rendered_value_containing_a_yaml_fence_does_not_end_the_block(okf_export):
    """A `---` inside a VALUE must not terminate the emitted frontmatter.

    Asserted at the render boundary rather than through a source file: the
    hand-rolled source parser closes its block at the first line that strips
    to `---`, so a fenced value cannot be delivered through a fixture. What
    this locks is the half the exporter owns — whatever value arrives, the
    block it writes stays one parseable unit.
    """
    concept = {
        "okf_type": "doc",
        "title": "Heading\n---\nTrailer",
        "description": "",
        "resource": "docs/x.md",
        "timestamp": "",
        "tags": [],
        "body": "Body.\n",
    }
    rendered = okf_export._render_concept(concept)
    _, block, body = rendered.split("---\n", 2)
    assert yaml.safe_load(block)["title"] == "Heading\n---\nTrailer"
    assert body == "Body.\n"


def test_tag_containing_comma_does_not_split_into_two_tags(okf_export, hostile_root, tmp_path):
    """`final, not sent` is ONE tag, not two."""
    out = tmp_path / "bundle-hostile-comma"
    okf_export.write_bundle(hostile_root, out, "user")
    tags = _pyyaml_frontmatter(out / "task" / "comma-tag.md")["tags"]
    assert "final, not sent" in tags
    assert len(tags) == 2  # the status value plus the context value, never three


def test_tag_containing_bracket_does_not_corrupt_the_list(okf_export, hostile_root, tmp_path):
    """A `]` inside a tag must not close the flow sequence early."""
    out = tmp_path / "bundle-hostile-bracket"
    okf_export.write_bundle(hostile_root, out, "user")
    tags = _pyyaml_frontmatter(out / "task" / "bracket-tag.md")["tags"]
    assert "blocked [see issue 42]" in tags


def test_every_emitted_concept_frontmatter_parses_with_pyyaml(okf_export, hostile_root, tmp_path):
    """OKF conformance clause 1, swept over a whole hostile bundle."""
    out = tmp_path / "bundle-hostile-sweep"
    okf_export.write_bundle(hostile_root, out, "user")
    concepts = [p for p in out.rglob("*.md") if p.name != "index.md"]
    assert concepts, "fixture produced no concept files"
    for path in concepts:
        fm = _pyyaml_frontmatter(path)
        assert isinstance(fm, dict), path
        assert fm.get("type"), f"{path} has no non-empty type"  # clause 2


# --------------------------------------------------------------------------
# main (CLI)
# --------------------------------------------------------------------------

def test_main_cli_success_writes_bundle_and_returns_zero(okf_export, bridge_root, tmp_path):
    out = tmp_path / "cli-bundle"
    rc = okf_export.main(["--root", str(bridge_root), "--out", str(out), "--scope", "user"])
    assert rc == 0
    assert (out / "index.md").exists()


def test_main_cli_missing_root_returns_nonzero(okf_export, tmp_path):
    missing_root = tmp_path / "does-not-exist"
    rc = okf_export.main(["--root", str(missing_root), "--out", str(tmp_path / "cli-out"), "--scope", "user"])
    assert rc != 0


def test_main_cli_rejects_unknown_scope_via_argparse(okf_export, bridge_root, tmp_path):
    with pytest.raises(SystemExit):
        okf_export.main(["--root", str(bridge_root), "--out", str(tmp_path / "cli-bogus"), "--scope", "bogus"])
