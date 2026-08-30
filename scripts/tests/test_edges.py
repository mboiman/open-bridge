#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/check-edges.py.

CONTRACT, this file is the authoritative spec for that surface.

WHY THIS EXISTS. The card layer says WHAT exists. The reachability contract
says a session can still find it. Neither says anything about the third thing
a Bridge is made of: the references BETWEEN entries. A customer names its
mandant, a mandant names a persona, an account names the task that provisioned
it, a workload names the machine it runs on. Those are declared, in ordinary
fields, and nothing has ever checked that they resolve.

Measured on a live instance before this existed: 126 repo-internal references
across 44 config files, 45 of them pointing at nothing. A third of the graph.

    THE INTERESTING HALF is not that they rot. It is HOW.

    Four of the dead ones, sampled: every single target still existed. The
    task had simply moved KIND — `work/tasks/<slug>` to `work/streams/<slug>`
    or to `work/done/YYYY-MM/<slug>` — which is the documented lifecycle,
    performed with `mv`, and nothing pulls the references along. The nodes are
    alive and the edges point at where they used to be.

    So a plain "does the path exist" check would report 45 broken links and
    tell you nothing about what to do. This one distinguishes:

        ok        resolves
        moved     the target lives, in another bucket — the fix is mechanical
        external  the first segment is not a directory of this repo at all,
                  so it belongs to a neighbour repo and is not ours to check
        dead      nothing anywhere

    TEMPLATES ARE NOT EDGES. `ecosystem.example.yaml` describes the tree a
    future instance will have; `_template.yaml` describes a file nobody has
    written yet. Checking their paths reports three findings in open-bridge
    and all three are correct-as-written.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


edges = _load("check_edges", "scripts/check-edges.py")


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ------------------------------------------------------------ extraction --


def test_a_repo_relative_path_in_a_value_is_an_edge(tmp_path):
    _tree(tmp_path, {
        "identity/mandants/a.yaml": "persona_ref: identity/personas/p.yaml\n",
        "identity/personas/p.yaml": "name: p\n",
    })
    found = edges.iter_edges(tmp_path)

    assert ("identity/mandants/a.yaml", "persona_ref",
            "identity/personas/p.yaml") in found


def test_an_edge_is_found_at_any_depth(tmp_path):
    _tree(tmp_path, {
        "workflow/projects/a.yaml": (
            "bridge_refs:\n  mandant: identity/mandants/m.yaml\n"
        ),
        "identity/mandants/m.yaml": "x: 1\n",
    })
    keys = [k for _, k, _ in edges.iter_edges(tmp_path)]

    assert "bridge_refs.mandant" in keys


def test_an_edge_inside_a_list_is_found(tmp_path):
    _tree(tmp_path, {
        "infra/channels/c.yaml": (
            "related:\n  - infra/remotes/r.yaml\n  - infra/remotes/s.yaml\n"
        ),
        "infra/remotes/r.yaml": "x: 1\n",
        "infra/remotes/s.yaml": "x: 1\n",
    })
    assert len(edges.iter_edges(tmp_path)) == 2


def test_ordinary_prose_is_not_an_edge(tmp_path):
    _tree(tmp_path, {
        "identity/mandants/a.yaml": (
            'note: "see the mandant docs"\n'
            'display_name: "Some Customer GmbH"\n'
            'url: "https://example.org/a.yaml"\n'
        ),
    })
    assert edges.iter_edges(tmp_path) == []


def test_a_template_is_not_scanned(tmp_path):
    """A template describes a file nobody has written yet."""
    _tree(tmp_path, {
        "identity/mandants/_template.yaml": "persona_ref: identity/personas/x.yaml\n",
    })
    assert edges.iter_edges(tmp_path) == []


def test_an_example_file_is_not_scanned(tmp_path):
    _tree(tmp_path, {"ecosystem.example.yaml": "files:\n  current: work/log.md\n"})
    assert edges.iter_edges(tmp_path) == []


def test_a_lock_file_is_not_scanned(tmp_path):
    """Generated state, with a src/dest for every materialized file."""
    _tree(tmp_path, {"overlays.lock.yaml": "o:\n  - src: a/b.yaml\n    dest: c/d.yaml\n"})
    assert edges.iter_edges(tmp_path) == []


# -------------------------------------------------------- classification --


def test_a_resolving_edge_is_ok(tmp_path):
    _tree(tmp_path, {"identity/personas/p.yaml": "x: 1\n"})
    assert edges.classify(tmp_path, "identity/personas/p.yaml") == ("ok", None)


def test_a_path_into_a_directory_this_repo_lacks_is_external(tmp_path):
    """A neighbour repo. Not ours to check, and not a finding.

    `wiki/bks-lab/standards/...` on a live instance points into a sibling
    checkout. Reporting it as broken would be reporting on someone else's tree,
    and after the third false alarm nobody reads the output.
    """
    _tree(tmp_path, {"identity/personas/p.yaml": "x: 1\n"})
    assert edges.classify(tmp_path, "wiki/standards/x.md")[0] == "external"


def test_a_missing_target_under_an_existing_family_is_dead(tmp_path):
    _tree(tmp_path, {"identity/personas/p.yaml": "x: 1\n"})
    assert edges.classify(tmp_path, "identity/personas/gone.yaml")[0] == "dead"


def test_a_task_that_moved_to_done_is_moved_not_dead(tmp_path):
    """THE case this exists for. The node lives; the edge is stale."""
    _tree(tmp_path, {"work/done/2026-05/migration/STATUS.md": "x\n"})
    state, target = edges.classify(tmp_path, "work/tasks/migration/STATUS.md")

    assert state == "moved"
    assert target == "work/done/2026-05/migration/STATUS.md"


def test_a_task_that_became_a_stream_is_moved(tmp_path):
    _tree(tmp_path, {"work/streams/praxis/STATUS.md": "x\n"})
    state, target = edges.classify(tmp_path, "work/tasks/praxis/STATUS.md")

    assert state == "moved"
    assert target == "work/streams/praxis/STATUS.md"


def test_a_deep_file_inside_a_moved_task_is_found(tmp_path):
    _tree(tmp_path, {"work/done/2026-07/x/deliverables/note.md": "x\n"})
    state, target = edges.classify(
        tmp_path, "work/tasks/x/deliverables/note.md"
    )

    assert state == "moved"
    assert target == "work/done/2026-07/x/deliverables/note.md"


def test_a_task_that_exists_nowhere_is_dead_not_moved(tmp_path):
    _tree(tmp_path, {"work/tasks/other/STATUS.md": "x\n"})
    assert edges.classify(tmp_path, "work/tasks/ghost/STATUS.md")[0] == "dead"


# ---------------------------------------------------------------- checks --


def test_check_reports_dead_and_moved_and_stays_quiet_on_ok(tmp_path):
    _tree(tmp_path, {
        "identity/mandants/a.yaml": (
            "persona_ref: identity/personas/p.yaml\n"
            "task_ref: work/tasks/moved/STATUS.md\n"
            "gone_ref: identity/personas/nope.yaml\n"
            "away_ref: wiki/x/y.md\n"
        ),
        "identity/personas/p.yaml": "x: 1\n",
        "work/streams/moved/STATUS.md": "x\n",
    })
    findings = edges.check(tmp_path)

    assert any("nope.yaml" in f and "dead" in f for f in findings)
    assert any("work/streams/moved/STATUS.md" in f for f in findings)
    assert not any("personas/p.yaml" in f for f in findings)
    assert not any("wiki/" in f for f in findings)


def test_stats_counts_by_class(tmp_path):
    _tree(tmp_path, {
        "identity/mandants/a.yaml": (
            "a: identity/personas/p.yaml\n"
            "b: identity/personas/nope.yaml\n"
            "c: wiki/x.md\n"
        ),
        "identity/personas/p.yaml": "x: 1\n",
    })
    stats = edges.stats(tmp_path)

    assert stats["ok"] == 1 and stats["dead"] == 1 and stats["external"] == 1


# ----------------------------------------------------------- neighbours --


def test_neighbours_lists_edges_out_of_a_node(tmp_path):
    _tree(tmp_path, {
        "identity/mandants/a.yaml": "persona_ref: identity/personas/p.yaml\n",
        "identity/personas/p.yaml": "x: 1\n",
    })
    out, _ = edges.neighbours(tmp_path, "identity/mandants/a.yaml")

    assert ("persona_ref", "identity/personas/p.yaml", "ok") in out


def test_neighbours_lists_edges_into_a_node(tmp_path):
    """The half a grep does not give you cheaply: who points HERE."""
    _tree(tmp_path, {
        "identity/mandants/a.yaml": "persona_ref: identity/personas/p.yaml\n",
        "workflow/projects/x.yaml": "owner: identity/personas/p.yaml\n",
        "identity/personas/p.yaml": "x: 1\n",
    })
    _, incoming = edges.neighbours(tmp_path, "identity/personas/p.yaml")
    sources = sorted(src for src, _ in incoming)

    assert sources == ["identity/mandants/a.yaml", "workflow/projects/x.yaml"]


# ----------------------------------------------------------------- --fix --


def test_fix_rewrites_a_moved_reference_and_keeps_the_comments(tmp_path):
    """Text substitution, not a YAML round trip.

    Re-serializing would drop every comment in the file, which is a far larger
    change than the one being made and would hide it in the diff.
    """
    _tree(tmp_path, {
        "identity/mandants/a.yaml": (
            "# why this mandant matters\n"
            "task_ref: work/tasks/moved/STATUS.md   # the launch plan\n"
        ),
        "work/done/2026-05/moved/STATUS.md": "x\n",
    })
    changed = edges.fix_moved(tmp_path)
    text = (tmp_path / "identity/mandants/a.yaml").read_text(encoding="utf-8")

    assert changed == 1
    assert "work/done/2026-05/moved/STATUS.md" in text
    assert "# why this mandant matters" in text
    assert "# the launch plan" in text


def test_fix_leaves_dead_references_alone(tmp_path):
    """A dead edge is a decision, not a typo. Only `moved` is mechanical."""
    _tree(tmp_path, {"identity/mandants/a.yaml": "x: identity/personas/nope.yaml\n"})
    assert edges.fix_moved(tmp_path) == 0


def test_fix_is_idempotent(tmp_path):
    _tree(tmp_path, {
        "identity/mandants/a.yaml": "task_ref: work/tasks/moved/STATUS.md\n",
        "work/streams/moved/STATUS.md": "x\n",
    })
    assert edges.fix_moved(tmp_path) == 1
    assert edges.fix_moved(tmp_path) == 0


# --------------------------------------------------------------- the CLI --


def test_main_runs_on_this_repo_and_agrees_with_itself():
    """A SMOKE test on real content, not a cleanliness gate.

    It used to assert the exit code was 0, which is a GATE and not a contract:
    it says this particular tree has no dead reference. In open-bridge that is
    trivially true, because CORE ships no instance YAML and therefore declares
    zero edges. In a downstream instance it is false the moment somebody
    references a document they have not written yet — and then the whole CORE
    contract suite goes red for a content decision that has nothing to do with
    whether this code works. Measured on one: one legitimate finding, 913 other
    tests passing, suite red.

    The gate exists and belongs where it already is: `validate.yml` runs
    `python3 scripts/check-edges.py` as its own step, so a real finding still
    fails CI, and it fails it with a name that says what broke.

    What survives here is what a contract test can honestly assert about a live
    tree: the CLI runs, and its exit code AGREES with its findings. That still
    catches the crash class this file was written after (a parser that walked
    real YAML and reported 54 findings, none of them real), and it stays green
    while an instance decides what to do about a reference.
    """
    findings = edges.check(edges.HERE.parent)
    assert edges.main([]) == (1 if findings else 0)
    assert all(isinstance(f, str) and f for f in findings)


def test_main_returns_one_on_a_finding(tmp_path):
    _tree(tmp_path, {"identity/mandants/a.yaml": "x: identity/personas/nope.yaml\n",
                     "identity/personas/p.yaml": "y: 1\n"})
    assert edges.main(["--repo-root", str(tmp_path)]) == 1


def test_main_stats_succeeds(tmp_path, capsys):
    _tree(tmp_path, {"identity/personas/p.yaml": "x: 1\n"})
    assert edges.main(["--repo-root", str(tmp_path), "--stats"]) == 0
    assert "edge" in capsys.readouterr().out


def test_main_neighbours_prints_both_directions(tmp_path, capsys):
    _tree(tmp_path, {
        "identity/mandants/a.yaml": "persona_ref: identity/personas/p.yaml\n",
        "identity/personas/p.yaml": "x: 1\n",
    })
    assert edges.main([
        "--repo-root", str(tmp_path), "--neighbours", "identity/personas/p.yaml"
    ]) == 0
    out = capsys.readouterr().out

    assert "identity/mandants/a.yaml" in out


# ---------------------------------------------------- declared exceptions --
#
# A checker over free-form YAML cannot know that `bin/generate_voice.py` is a
# runtime path inside a deployed pipeline, or that everything under a
# `family_repo:` key lives in a different checkout. On a live instance those
# two shapes were 7 of 18 "dead" findings — and a check whose output is mostly
# unactionable is a check people stop reading.
#
# So the instance says so, once, WITH A REASON. The reason is the point: it
# turns eighteen unknowns into eighteen known things.


def test_an_exception_silences_a_matching_finding(tmp_path):
    _tree(tmp_path, {
        "infra/channels/c.yaml": "pipeline:\n  steps:\n    - script: bin/x.py\n",
        # `bin/` has to EXIST, or the path classifies as external and there is
        # nothing for the exception to excuse.
        "bin/other.py": "x = 1\n",
        "edges.yaml": (
            "exceptions:\n"
            "  - path: infra/channels/c.yaml\n"
            "    keys: [pipeline.steps]\n"
            "    reason: runtime paths inside the deployed pipeline\n"
        ),
    })
    assert edges.check(tmp_path) == []


def test_an_exception_does_not_silence_a_different_key(tmp_path):
    _tree(tmp_path, {
        "infra/channels/c.yaml": (
            "pipeline:\n  steps:\n    - script: bin/x.py\n"
            "other_ref: infra/remotes/gone.yaml\n"
        ),
        "infra/remotes/r.yaml": "x: 1\n",
        "edges.yaml": (
            "exceptions:\n"
            "  - path: infra/channels/c.yaml\n"
            "    keys: [pipeline.steps]\n"
            "    reason: runtime paths\n"
        ),
    })
    findings = edges.check(tmp_path)

    assert any("gone.yaml" in f for f in findings)
    assert not any("bin/x.py" in f for f in findings)


def test_an_exception_does_not_silence_a_different_file(tmp_path):
    _tree(tmp_path, {
        "infra/channels/c.yaml": "a: infra/remotes/gone.yaml\n",
        "infra/channels/d.yaml": "a: infra/remotes/gone.yaml\n",
        "infra/remotes/r.yaml": "x: 1\n",
        "edges.yaml": (
            "exceptions:\n"
            "  - path: infra/channels/c.yaml\n"
            "    keys: [a]\n"
            "    reason: whatever\n"
        ),
    })
    findings = edges.check(tmp_path)

    assert len(findings) == 1
    assert "infra/channels/d.yaml" in findings[0]


def test_an_exception_without_a_reason_is_itself_a_finding(tmp_path):
    """An undocumented exception is indistinguishable from forgetting.

    The whole value of declaring one is the sentence next to it; without that
    the list becomes a place where findings go to be forgotten quietly.
    """
    _tree(tmp_path, {
        "infra/channels/c.yaml": "a: infra/remotes/gone.yaml\n",
        "infra/remotes/r.yaml": "x: 1\n",
        "edges.yaml": "exceptions:\n  - path: infra/channels/c.yaml\n    keys: [a]\n",
    })
    findings = edges.check(tmp_path)

    assert any("reason" in f for f in findings), findings


def test_an_exception_for_something_that_resolves_is_a_finding(tmp_path):
    """An exception that no longer excuses anything is stale, and stale
    exceptions are how a list of them turns into a list of lies."""
    _tree(tmp_path, {
        "infra/channels/c.yaml": "a: infra/remotes/r.yaml\n",
        "infra/remotes/r.yaml": "x: 1\n",
        "edges.yaml": (
            "exceptions:\n"
            "  - path: infra/channels/c.yaml\n"
            "    keys: [a]\n"
            "    reason: no longer needed\n"
        ),
    })
    assert any("excuses nothing" in f for f in edges.check(tmp_path))


def test_no_exception_file_is_fine(tmp_path):
    _tree(tmp_path, {"identity/personas/p.yaml": "x: 1\n"})
    assert edges.check(tmp_path) == []


def test_stats_counts_declared_exceptions_separately(tmp_path):
    _tree(tmp_path, {
        "infra/channels/c.yaml": "a: bin/x.py\n",
        "bin/other.py": "x = 1\n",
        "edges.yaml": (
            "exceptions:\n"
            "  - path: infra/channels/c.yaml\n"
            "    keys: [a]\n"
            "    reason: runtime path\n"
        ),
    })
    assert edges.stats(tmp_path)["declared"] == 1
