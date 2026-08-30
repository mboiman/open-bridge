#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Do the references between config entries still resolve?

    python3 scripts/check-edges.py                          # the gate
    python3 scripts/check-edges.py --stats                  # how dense, how broken
    python3 scripts/check-edges.py --neighbours <path>      # one hop, both ways
    python3 scripts/check-edges.py --fix                    # rewrite the moved ones

Exceptions live in an optional `edges.yaml` at the repo root, each with a
reason. See `load_exceptions`.

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


EXCEPTION_FILE = "edges.yaml"


def load_exceptions(repo_root) -> list[dict]:
    """Per-instance exceptions, each carrying its reason.

    A checker over free-form YAML cannot know that `bin/generate_voice.py` is a
    runtime path inside a deployed pipeline, or that everything under a
    `family_repo:` key lives in a different checkout. On a live instance those
    two shapes were 7 of 18 findings, and a check whose output is mostly
    unactionable is a check people stop reading.

    So the instance says so, once, WITH A REASON — and the reason is the point.
    It turns unknowns into known things. An exception without one is a finding,
    because an undocumented exception is indistinguishable from forgetting, and
    an exception that no longer excuses anything is a finding too, because that
    is how a list of exceptions becomes a list of lies.
    """
    if yaml is None:  # pragma: no cover
        return []
    path = Path(repo_root) / EXCEPTION_FILE
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("exceptions") or [])


def _excused(exceptions: list[dict], rel: str, key: str):
    """The exception covering this edge, or None."""
    for entry in exceptions:
        if entry.get("path") != rel:
            continue
        for prefix in entry.get("keys") or []:
            if key == prefix or key.startswith(prefix + "."):
                return entry
    return None


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


def _tail_match(root: Path, target: str):
    """A repo path ENDING in `target`, or None.

    The whole tail, never the basename alone. `README.md` exists all over a
    Bridge, and matching on the name would call every stale reference "moved"
    and point it somewhere arbitrary. `channels/telegram.yaml` matching
    `infra/channels/telegram.yaml` is a real move; `README.md` matching one of
    fourteen is a coin toss.
    """
    name = target.rsplit("/", 1)[-1]
    for path in root.rglob(name):
        # `.bridge/workspaces/` holds CLONES of other repositories. A hit there
        # resolves in somebody else's tree, which is the one thing this module
        # refuses to judge — and calling it a move would rewrite a reference
        # that is correct as written into a path that exists only while that
        # workspace happens to be checked out.
        if not path.is_file() or {".git", ".bridge"} & set(path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel != target and rel.endswith("/" + target):
            return rel
    return None


def classify(repo_root, target: str) -> tuple[str, str | None]:
    """ok | moved | external | dead, with the live path when it moved."""
    root = Path(repo_root)
    # An absolute or home-relative path IS the neighbour-checkout case, and it
    # used to be the one this missed: `"/Users/…".split("/")[0]` is `""`, and
    # `root / ""` is the root itself, which is always a directory — so it fell
    # past the escape hatch it was meant to take and came out `dead`.
    if target.startswith("/") or target.startswith("~"):
        return "external", None
    if (root / target).exists():
        return "ok", None

    # Before excusing anything: does this file live somewhere else in THIS tree?
    # The cluster-wrapper reorg moved channels/ backups/ remotes/ under infra/
    # and calendars/ contexts/ projects/ under workflow/, so a pre-reorg path
    # has a first segment that is no longer a repo directory — and the old rule
    # called exactly that "external" and excused it. It is the rot this guard
    # exists to find, and it was hiding in the guard's own escape hatch.
    moved = _tail_match(root, target)
    if moved:
        return "moved", moved

    head = target.split("/")[0]
    if not (root / head).is_dir():
        # A neighbour checkout. Judging it would be judging someone else's
        # tree, and the third false alarm is when people stop reading output.
        return "external", None

    work = _work_slug(target)
    if work:
        slug, tail = work
        for candidate in _work_candidates(root, slug, tail):
            if candidate != target and (root / candidate).exists():
                return "moved", candidate
    return "dead", None


def check(repo_root) -> list[str]:
    exceptions = load_exceptions(repo_root)
    used = set()
    findings = []
    for rel, key, target in iter_edges(repo_root):
        state, live = classify(repo_root, target)
        excuse = _excused(exceptions, rel, key)
        if excuse is not None and state in ("dead", "moved"):
            used.add(id(excuse))
            if not str(excuse.get("reason") or "").strip():
                findings.append(
                    f"{rel} :: {key} is excepted in {EXCEPTION_FILE} with no "
                    f"reason; an undocumented exception is indistinguishable "
                    f"from forgetting"
                )
            continue
        if state == "moved":
            findings.append(
                f"{rel} :: {key} -> {target} moved; it now lives at {live} "
                f"(`--fix` rewrites this one)"
            )
        elif state == "dead":
            findings.append(f"{rel} :: {key} -> {target} is dead, nothing at that path")

    for entry in exceptions:
        if id(entry) not in used:
            findings.append(
                f"{entry.get('path')} :: {entry.get('keys')} is excepted in "
                f"{EXCEPTION_FILE} and excuses nothing — every edge it names "
                f"resolves, so the exception is stale"
            )
    return findings


def stats(repo_root) -> Counter:
    exceptions = load_exceptions(repo_root)
    counter: Counter = Counter()
    for rel, key, target in iter_edges(repo_root):
        state = classify(repo_root, target)[0]
        if state in ("dead", "moved") and _excused(exceptions, rel, key):
            state = "declared"
        counter[state] += 1
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


def fix_moved(repo_root):
    """Rewrite references whose target moved bucket. Returns (changed, skipped).

    Text, not a YAML round trip: re-serializing would drop every comment in the
    file, a far larger change than the one being made and one that hides it in
    the diff. Only `moved` is rewritten — a dead reference is a decision
    somebody has to make, not a typo.

    AND ONLY WHAT IS NOT EXCEPTED. `check()` and `stats()` both honoured
    `edges.yaml`; this did not, so a path an instance had explicitly declared
    NOT to be repo-relative, with a written reason, was rewritten as if it were.

    It happened, on the run that found this: an instance excepted
    `pipeline.steps` with the reason "runtime paths of the deployed pipeline,
    relative to the SERVICE's working directory". The tail match found the
    sources in the repo and `--fix` rewrote `bin/generate_voice.py` into a path
    the service cannot resolve. A working config, broken by its own guard, and
    the exception then reported as stale because after the rewrite it excused
    nothing.

    Skips are RETURNED, never swallowed: a fix run that declines to touch
    something has to say so, or the next reader takes silence for "there was
    nothing to decline".
    """
    root = Path(repo_root)
    exceptions = load_exceptions(repo_root)
    changed, skipped = 0, []
    for rel, key, target in iter_edges(repo_root):
        state, live = classify(repo_root, target)
        if state != "moved" or not live:
            continue
        excuse = _excused(exceptions, rel, key)
        if excuse is not None:
            skipped.append(
                f"{rel} :: {key} -> {target} left alone: excepted in "
                f"{EXCEPTION_FILE} ({str(excuse.get('reason') or '').strip().splitlines()[0]})"
            )
            continue
        path = root / rel
        text = path.read_text(encoding="utf-8")
        if target not in text:
            continue
        path.write_text(text.replace(target, live), encoding="utf-8")
        changed += 1
    return changed, skipped


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
        for state in ("ok", "moved", "dead", "external", "declared"):
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
        changed, skipped = fix_moved(root)
        print(f"check-edges: rewrote {changed} moved reference(s)")
        for line in skipped:
            print(f"  skipped {line}")
        if skipped:
            print(
                f"check-edges: {len(skipped)} left alone because {EXCEPTION_FILE} "
                f"says they are not repo-relative"
            )
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
