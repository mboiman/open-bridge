#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measure the always-on context a session loads, and hold it to a declared cap.

Every session loads a fixed body of text before the first answer: the
`@`-imports named in CLAUDE.md, the standing orders carrying `scope: always`,
and the Phase 1 reads. Nothing measured it, so it only ever grew, and it grew
where nobody looks: roughly half arrives through tool reads rather than
imports, so it never appears in a client's context listing at all.

One file in this repo has never sprawled, `identity/agent/SOUL.md`, and the
only thing that distinguishes it is a declared cap that a validator enforces.
This script generalises that cap to the whole always-on surface.

    python3 scripts/measure-context.py                 # report + gate
    python3 scripts/measure-context.py --method api    # exact counts
    python3 scripts/measure-context.py --out FILE      # also write the report

Exit code 0 when every item is within policy, 1 otherwise.

WHAT GATES AND WHAT ONLY INFORMS. The gate is on **bytes**, which are exact,
offline, and identical on every machine. Tokens are reported beside them, and
carry the method that produced them: `api` is exact
(`messages.count_tokens`, model-specific), `bytes` is an estimate from a
declared calibration. An estimate never fails a build, and never wears the
label of an exact count. A budget figure that does not say what counted it is
not a measurement.

The full contract, including every state and why it exists, lives in
`scripts/tests/test_measure_context.py`, which is the spec for this file.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

CORE_BUDGET = "context-budget.yaml"
USER_BUDGET = "context-budget.user.yaml"
ENTRY = "CLAUDE.md"
ORDERS_DIR = Path("protocols") / "standing-orders"

# Only a line that STARTS with `@` is an import. An address in prose is not.
IMPORT_RE = re.compile(r"^@(\S+)\s*$")

DEFAULT_BYTES_PER_TOKEN = 2.4
DEFAULT_MODEL = "claude-opus-5"

# The states that fail the build. `uncapped` is deliberately absent: a newly
# declared item must be addable in one line without breaking anyone's CI.
FAILING = {"over", "undeclared", "missing"}


# ------------------------------------------------------------- the budget --

def load_budget(repo_root: Path) -> dict:
    """Read the CORE budget, then overlay the per-instance one when present.

    The overlay replaces an item wholesale by key. It is not a deep merge,
    because a half-overridden cap is worse than either cap on its own.
    """
    core = Path(repo_root) / CORE_BUDGET
    if not core.is_file():
        sys.exit(
            f"error: {CORE_BUDGET} not found in {repo_root}.\n"
            "       A meter with no policy enforces nothing and would report\n"
            "       green forever, so this is refused rather than skipped."
        )

    budget = yaml.safe_load(core.read_text(encoding="utf-8")) or {}
    budget.setdefault("items", {})
    budget.setdefault("calibration", {})
    budget["calibration"].setdefault("bytes_per_token", DEFAULT_BYTES_PER_TOKEN)
    budget["calibration"].setdefault("model", DEFAULT_MODEL)

    user = Path(repo_root) / USER_BUDGET
    if user.is_file():
        overlay = yaml.safe_load(user.read_text(encoding="utf-8")) or {}
        for key, value in (overlay.get("items") or {}).items():
            budget["items"][key] = value or {}
        budget["calibration"].update(overlay.get("calibration") or {})

    budget["items"] = {k: (v or {}) for k, v in (budget["items"] or {}).items()}
    return budget


# ---------------------------------------------------------- what is loaded --

def discover_imports(repo_root: Path, entry: str = ENTRY, max_depth: int = 4) -> list[str]:
    """The `@`-imports actually loaded, followed recursively, first-seen order.

    An import that does not resolve is RETURNED rather than dropped. Dropping
    it would remove it from the measured set silently, which is the exact
    failure this script exists to prevent.
    """
    root = Path(repo_root)
    found: list[str] = []
    seen: set[str] = set()

    def walk(rel: str, depth: int) -> None:
        if depth > max_depth:
            return
        path = root / rel
        if not path.is_file():
            return
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = IMPORT_RE.match(line)
            if not match:
                continue
            target = match.group(1)
            if target in seen:
                continue
            seen.add(target)
            found.append(target)
            walk(target, depth + 1)

    walk(entry, 0)
    return found


def _frontmatter(path: Path) -> dict:
    """The YAML block between the first two `---` fences, or an empty dict."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return {}
    body = parts[0][3:]
    try:
        loaded = yaml.safe_load(body)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def discover_standing_orders(repo_root: Path) -> list[str]:
    """Every standing order carrying `scope: always`, sorted by path."""
    base = Path(repo_root) / ORDERS_DIR
    if not base.is_dir():
        return []
    found = []
    for path in base.rglob("*.md"):
        if path.name == "README.md" or path.name.startswith("_"):
            continue
        if str(_frontmatter(path).get("scope", "")).strip() == "always":
            found.append(path.relative_to(repo_root).as_posix())
    return sorted(found)


# ------------------------------------------------------------ the counting --

def _api_token_count(text: str, model: str):
    """Exact count via `messages.count_tokens`, or None when unavailable.

    Credentials are left entirely to the SDK: an unset `ANTHROPIC_API_KEY`
    does not mean there are none, since a profile from `ant auth login` also
    authenticates a zero-argument client.
    """
    try:
        import anthropic
    except Exception:
        return None
    try:
        client = anthropic.Anthropic()
        response = client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": text}],
        )
        return int(response.input_tokens)
    except Exception:
        return None


def count_tokens(text: str, method: str, bytes_per_token: float, model: str):
    """Return (count, method_actually_used).

    A requested `api` that cannot run falls back to `bytes` and says `bytes`.
    Reporting an estimate as exact is the one outcome forbidden here.
    """
    if method == "api":
        exact = _api_token_count(text, model)
        if exact is not None:
            return exact, "api"
    size = len(text.encode("utf-8"))
    return int(round(size / float(bytes_per_token))), "bytes"


# ---------------------------------------------------------------- the rows --

def item_state(row: dict, policy, exists: bool) -> str:
    """ok | over | uncapped | undeclared | absent | missing."""
    if policy is None:
        # Discovered on disk (or declared as an import) but absent from the
        # budget. Without this the budget silently stops covering the tree the
        # moment somebody adds an import, and a meter that cannot see a new
        # file is worse than no meter: it reports green while its subject grows.
        return "undeclared"
    if not exists:
        return "absent" if policy.get("optional") else "missing"
    cap = policy.get("max_bytes")
    if cap is None:
        return "uncapped"
    return "over" if row["bytes"] > int(cap) else "ok"


def collect_rows(repo_root: Path, budget: dict, method: str) -> list[dict]:
    """One row per always-on item, sorted by (source, path)."""
    root = Path(repo_root)
    items = budget.get("items") or {}
    calibration = budget.get("calibration") or {}
    bytes_per_token = float(calibration.get("bytes_per_token", DEFAULT_BYTES_PER_TOKEN))
    model = str(calibration.get("model", DEFAULT_MODEL))

    discovered: dict[str, str] = {}
    if (root / ENTRY).is_file():
        discovered[ENTRY] = "import"
    for rel in discover_imports(root):
        discovered.setdefault(rel, "import")
    for rel in discover_standing_orders(root):
        discovered.setdefault(rel, "standing-order")

    rows = []
    for path in sorted(set(discovered) | set(items)):
        policy = items.get(path)
        source = discovered.get(path) or (policy or {}).get("source") or "phase1"
        target = root / path
        exists = target.is_file()

        row = {
            "path": path,
            "source": source,
            "bytes": 0,
            "tokens": 0,
            "method": method,
            "max_bytes": (policy or {}).get("max_bytes"),
        }
        if exists:
            text = target.read_text(encoding="utf-8", errors="replace")
            row["bytes"] = len(text.encode("utf-8"))
            row["tokens"], row["method"] = count_tokens(
                text, method, bytes_per_token, model
            )
        row["state"] = item_state(row, policy, exists)
        rows.append(row)

    rows.sort(key=lambda r: (r["source"], r["path"]))
    return rows


# -------------------------------------------------------------- the report --

def effective_method(rows: list[dict], requested: str) -> str:
    """`api` only when every measured row really got an exact count."""
    measured = [r for r in rows if r["bytes"] > 0]
    if requested == "api" and measured and all(r["method"] == "api" for r in measured):
        return "api"
    return "bytes"


def _cell(value) -> str:
    return "-" if value is None else f"{value:,}".replace(",", " ")


def render_report(rows: list[dict], method: str, bytes_per_token: float) -> str:
    """Deterministic markdown. No timestamp, no host, no absolute path."""
    out = ["# Context budget", ""]
    if method == "api":
        out.append("Token counts are exact (`messages.count_tokens`).")
    else:
        out.append(
            f"Token counts are an **estimate** (method `bytes`, "
            f"{bytes_per_token} bytes per token). Bytes are exact, and the caps "
            f"gate on bytes."
        )
    out += [
        "",
        "| Source | Path | Bytes | Cap | Tokens | State |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        out.append(
            f"| {row['source']} | `{row['path']}` | {_cell(row['bytes'])} | "
            f"{_cell(row['max_bytes'])} | {_cell(row['tokens'])} | {row['state']} |"
        )

    total_bytes = sum(r["bytes"] for r in rows)
    total_tokens = sum(r["tokens"] for r in rows)
    out.append(
        f"| | **total** | **{_cell(total_bytes)}** | | "
        f"**{_cell(total_tokens)}** | |"
    )

    failing = [r for r in rows if r["state"] in FAILING]
    out.append("")
    if failing:
        out.append(f"{len(failing)} item(s) outside policy:")
        out.append("")
        for row in failing:
            out.append(f"- `{row['path']}` — {row['state']}")
    else:
        out.append("Every item is within policy.")
    out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ main --

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure the always-on context and hold it to its budget."
    )
    parser.add_argument("--repo-root", default=None, help="defaults to this repo")
    parser.add_argument("--method", choices=("bytes", "api"), default="bytes")
    parser.add_argument("--out", default=None, help="also write the report here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parents[1]
    )
    budget = load_budget(root)
    rows = collect_rows(root, budget, args.method)

    bytes_per_token = float(
        (budget.get("calibration") or {}).get("bytes_per_token", DEFAULT_BYTES_PER_TOKEN)
    )
    report = render_report(rows, effective_method(rows, args.method), bytes_per_token)

    if not args.quiet:
        print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")

    return 1 if any(r["state"] in FAILING for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
