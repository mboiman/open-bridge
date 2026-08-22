#!/usr/bin/env python3
"""Generate work/board.md from the filesystem — the board is a VIEW, never hand-curated.

The board is derived from the task directories so its counts cannot drift from
reality (the wildwuchs failure mode it replaces). Run it after any status change
or task move; it is the canonical generator referenced by the
work-board-reconciliation standing order and /briefing.

Source of truth:
  work/tasks/<slug>/STATUS.md     finite tasks   (status: backlog|doing|review|done)
  work/streams/<slug>/STATUS.md   long-runners   (never 'done'; excluded from WIP)
  work/done/YYYY-MM/<slug>/       closed tasks    (current month shown)

Description per row: the STATUS.md `headline:` frontmatter field if present,
else the first markdown H1 (`# ...`) in the body, else the slug.

Usage: python3 scripts/gen-board.py [--check]
  (no args) regenerate work/board.md
  --check   print the would-be summary counts without writing (drift check)

Any other argument is a usage error and exits 2 WITHOUT writing. This script
regenerates a tracked file, so an unrecognised flag must never fall through to
the write path (`gen-board.py --help` used to regenerate the board).
"""
from __future__ import annotations
import datetime as _dt
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "work" / "tasks"
STREAMS = ROOT / "work" / "streams"
DONE = ROOT / "work" / "done"

WIP_WARN = 10  # mirrors bridge-config.yaml work.max_active (soft warning only)

USAGE = (
    "usage: python3 scripts/gen-board.py [--check]\n"
    "  (no args) regenerate work/board.md\n"
    "  --check   print the would-be summary counts without writing\n"
)


def _load_reference_parser():
    """Return scripts/okf-export.py's parse_frontmatter, the repo's tested one.

    Deliberately ONE implementation with two consumers instead of two
    hand-rolled parsers. This script carried its own for months and drifted
    away from YAML in four ways at once: a `#` inside a quoted value truncated
    it, `.strip('"')` ate a quote the value legitimately ended on, splitting
    the file on every `---` ended the frontmatter inside a value (taking every
    key after it with it), and a block scalar arrived as the bare `|`.
    parse_frontmatter is covered by scripts/tests/test_okf_export.py and
    agrees with PyYAML on well-formed input; this script's own contract lives
    in scripts/tests/test_gen_board.py.

    Loaded by path because the filename is hyphenated, and with bytecode
    writing suppressed so importing a sibling script leaves no __pycache__
    entry behind in a tracked directory.
    """
    path = ROOT / "scripts" / "okf-export.py"
    spec = importlib.util.spec_from_file_location("_bridge_frontmatter_source", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in a checkout
        raise RuntimeError(f"cannot load the frontmatter parser from {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module.parse_frontmatter


parse_frontmatter = _load_reference_parser()


def parse_status(md: Path) -> dict:
    """Parse a STATUS.md: frontmatter fields + a description.

    Frontmatter parsing is delegated (see _load_reference_parser); everything
    below is this script's own fallback chain for the board's columns.
    """
    text = md.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    desc = fm.get("headline", "").strip()
    if not desc:
        for line in body.splitlines():
            if line.startswith("# "):
                desc = line[2:].strip()
                break
    if not desc:
        desc = md.parent.name
    return {
        "slug": md.parent.name,
        "desc": desc,
        "type": fm.get("type", "—"),
        "context": fm.get("context", "—"),
        "since": fm.get("created", fm.get("last_updated", "—")),
        "status": fm.get("status", "?"),
        "blocked_by": fm.get("blocked_by", "").strip(),
    }


def collect(folder: Path) -> tuple[list[dict], list[str]]:
    out: list[dict] = []
    nostatus: list[str] = []
    if not folder.exists():
        return out, nostatus
    for d in sorted(p for p in folder.iterdir() if p.is_dir()):
        s = d / "STATUS.md"
        if s.exists():
            out.append(parse_status(s))
        else:
            nostatus.append(d.name)
    return out, nostatus


def status_cell(t: dict) -> str:
    return f"{t['status']} 🚧 blocked" if t["blocked_by"] else t["status"]


def cell(value: str) -> str:
    """Render one value into a markdown table cell.

    A raw `|` splits the row into extra columns and a newline ends it, so both
    are neutralised. Required rather than defensive: now that block scalars
    parse correctly, a `headline: |` legitimately carries newlines, and a
    headline naming a shell pipe or an alternation legitimately carries `|`.
    Backslash goes first so an escape this function adds cannot be cancelled
    by one already in the value.
    """
    flat = " ".join(str(value).split())
    return flat.replace("\\", "\\\\").replace("|", "\\|")


def row(t: dict) -> str:
    cells = (t["slug"], t["desc"], t["type"], t["context"], t["since"], status_cell(t))
    return "| " + " | ".join(cell(c) for c in cells) + " |"


def section(title: str, rows: list[dict]) -> str:
    head = f"## {title} ({len(rows)})\n\n| Ticket | Beschreibung | Typ | Context | Seit | Status |\n|---|---|---|---|---|---|\n"
    return head + ("\n".join(row(t) for t in rows) if rows else "_(leer)_") + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args != ["--check"]:
        sys.stderr.write(USAGE)
        return 2

    tasks, task_nostatus = collect(TASKS)
    streams, stream_nostatus = collect(STREAMS)
    doing = [t for t in tasks if t["status"] == "doing"]
    review = [t for t in tasks if t["status"] == "review"]
    backlog = [t for t in tasks if t["status"] == "backlog"]
    today = _dt.date.today()
    month = today.strftime("%Y-%m")
    done_dir = DONE / month
    done_dirs = sorted(p.name for p in done_dir.iterdir() if p.is_dir()) if done_dir.exists() else []
    wip = len(doing) + len(review)

    if args == ["--check"]:
        print(f"Doing {len(doing)} · Review {len(review)} · Backlog {len(backlog)} · "
              f"Streams {len(streams)} · Done-{month} {len(done_dirs)} · WIP {wip}/{WIP_WARN}")
        return 0

    warn = f" · ⚠️ WIP {wip} > {WIP_WARN}" if wip > WIP_WARN else ""
    nostatus = task_nostatus + [f"{n} (stream)" for n in stream_nostatus]
    out = []
    out.append("# Board\n")
    out.append(f"> Stand: {today.strftime('%d.%m.%Y')} · **generiert** via `scripts/gen-board.py` aus "
               "`work/tasks/` + `work/streams/` + `work/done/` — nicht von Hand pflegen "
               "(Counts leiten sich aus dem Filesystem ab, können nicht driften).\n")
    out.append("| Bucket | Count |\n|---|---|\n"
               f"| Doing (tasks) | {len(doing)} |\n"
               f"| Review (tasks) | {len(review)} |\n"
               f"| Backlog (tasks) | {len(backlog)} |\n"
               f"| Streams | {len(streams)} |\n"
               f"| Done — {month} | {len(done_dirs)} |\n"
               f"| WIP (doing+review) | {wip} / {WIP_WARN}{warn} |\n"
               + (f"| No-STATUS dirs | {len(nostatus)} ({', '.join(nostatus)}) |\n" if nostatus else ""))
    out.append("")
    out.append(section("Doing", doing))
    out.append(section("Review", review))
    out.append(section("Backlog", backlog))
    out.append(section("Streams", streams))
    out.append(f"## Done — {month} ({len(done_dirs)})\n\n" + (", ".join(f"`{d}`" for d in done_dirs) if done_dirs else "_(leer)_") + "\n")
    (ROOT / "work" / "board.md").write_text("\n".join(out), encoding="utf-8")
    print(f"board.md regenerated: Doing {len(doing)} · Review {len(review)} · Backlog {len(backlog)} · "
          f"Streams {len(streams)} · Done-{month} {len(done_dirs)} · WIP {wip}/{WIP_WARN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
