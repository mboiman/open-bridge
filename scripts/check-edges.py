#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Do the references between config entries still resolve?

    python3 scripts/check-edges.py                          # the gate
    python3 scripts/check-edges.py --stats                  # how dense, how broken
    python3 scripts/check-edges.py --neighbours <path>      # one hop, both ways
    python3 scripts/check-edges.py --fix                    # rewrite the moved ones

The card layer says WHAT exists. The reachability contract says a session can
still find it. Neither says anything about the third thing a Bridge is made of:
the references BETWEEN entries. A customer names its mandant, a mandant names a
persona, an account names the task that provisioned it, a workload names the
machine it runs on. They are declared, in ordinary fields, and nothing has ever
checked that they resolve.

Measured on a live instance before this existed: 126 repo-internal references
across 44 config files, 45 of them pointing at nothing.

THE INTERESTING HALF IS NOT THAT THEY ROT, IT IS HOW. Four of the dead ones,
sampled: every target still existed. The task had moved KIND —
`work/tasks/<slug>` to `work/streams/<slug>` or `work/done/YYYY-MM/<slug>` —
which is the documented lifecycle, performed with `mv`, and nothing pulls the
references along. The nodes are alive and the edges point at where they used to
be. A plain existence check would report 45 broken links and tell you nothing
about what to do with them, so this one separates:

    ok        resolves
    moved     the target lives, in another bucket; the fix is mechanical
    external  the first segment is not a directory of this repo at all, so the
              path belongs to a neighbour checkout and is not ours to judge
    dead      nothing anywhere

TEMPLATES ARE NOT EDGES. `_template.yaml` describes a file nobody has written
and `*.example.yaml` describes the tree a future instance will have. Their
paths are correct as written and reporting them would be three false alarms in
open-bridge alone, which is how a checker teaches people to ignore it.

Contract: `scripts/tests/test_edges.py`.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a documented dependency
    yaml = None

HERE = Path(__file__).resolve().parent

# A repo-relative path in a scalar. Anchored, so a URL (which carries `://`)
# and ordinary prose (no slash, no extension) never match.
EDGE = re.compile(
    r"^[a-z_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.@+-]+)+\.(?:yaml|yml|md|py|sh)$"
)

SCAN_GLOBS = (
    "*.yaml",
    "identity/*/*.yaml",
    "infra/*/*.yaml",
    "workflow/*/*.yaml",
    "trackers/*.yaml",
)

# Where a work item can live. The KIND model in AGENTS.md: a finite task, a
# long-runner, or closed under a month.
WORK_BUCKETS = ("work/tasks", "work/streams")


def _skip(rel: str) -> bool:
    name = Path(rel).name
    return (
        name.startswith("_")
        or name.endswith(".example.yaml")
        or name.endswith(".lock.yaml")
        or rel.startswith(".bridge/")
        or "/examples/" in rel
    )


def _walk(node, path: list[str], out: list):
    if isinstance(node, dict):
        for key, value in node.items():
            _walk(value, path + [str(key)], out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, path + [str(index)], out)
    elif isinstance(node, str) and EDGE.match(node.strip()):
        out.append((".".join(path), node.strip()))


def iter_edges(repo_root) -> list[tuple[str, str, str]]:
    """Every declared repo-relative reference, as (file, key path, target)."""
    if yaml is None:  # pragma: no cover
        return []
    root = Path(repo_root)
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for pattern in SCAN_GLOBS:
        for path in sorted(root.glob(pattern)):
            rel = str(path.relative_to(root))
            if rel in seen or _skip(rel):
                continue
            seen.add(rel)
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
            hits: list = []
            _walk(doc, [], hits)
            found.extend((rel, key, target) for key, target in hits)
    return found


def _work_slug(target: str):
    """(slug, tail) for a `work/...` path, or None when it is not one."""
    parts = target.split("/")
    if len(parts) < 3 or parts[0] != "work":
        return None
    if parts[1] == "done":
        if len(parts) < 4:
            return None
        return parts[3], parts[4:]
    return parts[2], parts[3:]


def _work_candidates(root: Path, slug: str, tail: list[str]) -> list[str]:
    rest = "/".join(tail)
    out = []
    for bucket in WORK_BUCKETS:
        out.append(f"{bucket}/{slug}" + (f"/{rest}" if rest else ""))
    done = root / "work" / "done"
    if done.is_dir():
        for month in sorted(p.name for p in done.iterdir() if p.is_dir()):
            out.append(f"work/done/{month}/{slug}" + (f"/{rest}" if rest else ""))
    return out


def classify(repo_root, target: str) -> tuple[str, str | None]:
    """ok | moved | external | dead, with the live path when it moved."""
    root = Path(repo_root)
    head = target.split("/")[0]
    if not (root / head).is_dir():
        # A neighbour checkout. Judging it would be judging someone else's
        # tree, and the third false alarm is when people stop reading output.
        return "external", None
    if (root / target).exists():
        return "ok", None

    work = _work_slug(target)
    if work:
        slug, tail = work
        for candidate in _work_candidates(root, slug, tail):
            if candidate != target and (root / candidate).exists():
                return "moved", candidate
    return "dead", None


def check(repo_root) -> list[str]:
    findings = []
    for rel, key, target in iter_edges(repo_root):
        state, live = classify(repo_root, target)
        if state == "moved":
            findings.append(
                f"{rel} :: {key} -> {target} moved; it now lives at {live} "
                f"(`--fix` rewrites this one)"
            )
        elif state == "dead":
            findings.append(f"{rel} :: {key} -> {target} is dead, nothing at that path")
    return findings


def stats(repo_root) -> Counter:
    counter: Counter = Counter()
    for _, _, target in iter_edges(repo_root):
        counter[classify(repo_root, target)[0]] += 1
    return counter


def neighbours(repo_root, node: str):
    """(outgoing, incoming) for one node — one hop, both directions.

    The incoming half is the one a grep does not hand you cheaply, and it is
    the half that answers "what breaks if I move this".
    """
    outgoing, incoming = [], []
    for rel, key, target in iter_edges(repo_root):
        if rel == node:
            outgoing.append((key, target, classify(repo_root, target)[0]))
        if target == node:
            incoming.append((rel, key))
    return outgoing, incoming


def fix_moved(repo_root) -> int:
    """Rewrite references whose target moved bucket. Text, not a YAML round trip.

    Re-serializing would drop every comment in the file: a far larger change
    than the one being made, and one that hides it in the diff. Only `moved` is
    rewritten — a dead reference is a decision somebody has to make, not a typo.
    """
    root = Path(repo_root)
    changed = 0
    for rel, _, target in iter_edges(repo_root):
        state, live = classify(repo_root, target)
        if state != "moved" or not live:
            continue
        path = root / rel
        text = path.read_text(encoding="utf-8")
        if target not in text:
            continue
        path.write_text(text.replace(target, live), encoding="utf-8")
        changed += 1
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo-root", default=str(HERE.parent))
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--neighbours", metavar="PATH")
    parser.add_argument("--fix", action="store_true", help="rewrite moved references")
    args = parser.parse_args(argv)
    root = Path(args.repo_root)

    if args.stats:
        counter = stats(root)
        total = sum(counter.values())
        print(f"check-edges: {total} declared edge(s)")
        for state in ("ok", "moved", "dead", "external"):
            if counter[state]:
                print(f"  {counter[state]:>4}  {state}")
        return 0

    if args.neighbours:
        outgoing, incoming = neighbours(root, args.neighbours)
        print(f"# {args.neighbours}")
        print(f"\nout ({len(outgoing)}):")
        for key, target, state in outgoing:
            print(f"  {key} -> {target}  [{state}]")
        print(f"\nin ({len(incoming)}):")
        for source, key in incoming:
            print(f"  {source} :: {key}")
        return 0

    if args.fix:
        changed = fix_moved(root)
        print(f"check-edges: rewrote {changed} moved reference(s)")
        return 0

    findings = check(root)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"check-edges: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"check-edges: {len(iter_edges(root))} declared edge(s), all resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
