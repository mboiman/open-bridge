"""Pipeline config — the per-project `pipeline:` block from
workflow/projects/<slug>.yaml, parsed into an EngineConfig.

The engine is generic; ALL project specifics (which stages, which handlers, the
trigger column) live in config. A heavy project (a compiled product) and a light one (docs) use
the SAME engine with different config.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .interfaces import Stage

# --- transparency: the closed sets -----------------------------------------
# Every value a human can type into the `record:`/`evidence:` blocks is checked
# against one of these. The loader below builds EngineConfig from a FIXED keyword
# set and never populates `extra`, so an unrecognised knob is not a no-op with a
# warning — it is invisible. Silently ignoring `require_issue` arms draft cards
# and burns an LLM run on an item that can carry no `Closes #N` and no comment.
_RECORD_EVENTS = ("armed", "stage", "reject", "park", "gate")
_RECORD_SCAN_MODES = ("redact", "off")
_RECORD_KEYS = ("enabled", "events", "sticky_marker", "templates_dir", "max_body_chars", "scan")
_EVIDENCE_KEYS = ("dir", "require_deterministic")


@dataclass
class RecordConfig:
    """The record layer's knobs. `enabled: False` = today's engine, which writes
    no run record at all."""

    enabled: bool = False
    events: tuple = _RECORD_EVENTS
    sticky_marker: str = "board-pilot:run"
    templates_dir: str | None = None
    max_body_chars: int = 60000       # GitHub's hard ceiling is 65536
    scan: str = "redact"              # redact | off — the PR body is fail-closed regardless


@dataclass
class EvidenceConfig:
    dir: str | None = None
    # Fail-closed default: an agent stage reporting on itself is not evidence, it
    # is a claim. Safe to default ON because no config declares per-stage
    # `evidence:` today, so this refuses nothing that works now.
    require_deterministic: bool = True


@dataclass
class EngineConfig:
    stages: list
    trigger_status: str = "Todo"
    pr_status: str = "In Review"      # status set after the gated PR stage
    done_status: str = "Done"         # the engine MUST NEVER set this (human-only)
    concurrency: int = 1
    token_ceiling: int | None = None
    project: str = "test"
    bounce_field: str = "Bounces"     # board Number field — durable reject counter
    max_rounds: int | None = None     # rework.max_rounds default (per-edge override on the stage)
    extra: dict = field(default_factory=dict)
    # --- transparency ----------------------------------------------------
    # All default to today's behaviour: None means "the engine does not write
    # this column", so landing these knobs changes no pipeline already running.
    working_status: str | None = None   # written at ARM, so a taken card leaves the free column
    park_status: str | None = None      # written at EVERY park, so a parked card stops looking fresh
    require_issue: bool = False         # opt-in: refuse to arm a card with no issue behind it
    record: RecordConfig = field(default_factory=RecordConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)


def _on_success(raw) -> str:
    """on_success may be a bare string or a mapping like {set_pipeline: implementing}."""
    if isinstance(raw, dict):
        return raw.get("set_pipeline") or raw.get("set_status") or ""
    return raw or ""


def validate_chain(stages: list, rework_max_rounds=None) -> None:
    """Fail LOUD at load if any transition target is unresolvable.

    Builds the pipeline before-key set (the ``queued`` entry plus each stage's
    ``on_success``) and the stage-id→index map, then checks:

    * every ``on_success`` is non-empty (it defines the chain);
    * every ``rewind_to`` resolves to a real before-key or the terminal value;
    * every ``reject_to`` (the reject edge) names a real stage id that is
      STRICTLY UPSTREAM of the rejecting stage, with a resolvable positive
      ``max_rounds`` (per-edge override, else ``rework.max_rounds``).

    Raises ``ValueError`` naming the offending stage AND target — never a silent
    runtime stall.
    """
    if not stages:
        return
    before_keys = set()
    idx_by_id: dict = {}
    before = "queued"
    for i, s in enumerate(stages):
        before_keys.add(before)
        idx_by_id[s.id] = i
        before = s.on_success
    terminal = stages[-1].on_success
    valid_targets = before_keys | {terminal}

    for i, s in enumerate(stages):
        if not s.on_success:
            raise ValueError(f"stage {s.id!r}: empty on_success — every stage must name its next pipeline state")
        if s.on_fail == "rewind" and s.rewind_to is not None:
            if s.rewind_to not in valid_targets:
                raise ValueError(
                    f"stage {s.id!r}: rewind target {s.rewind_to!r} resolves to no pipeline state; "
                    f"valid before-keys: {sorted(valid_targets)}"
                )
        reject_to = getattr(s, "reject_to", None)
        if reject_to is not None:
            if reject_to not in idx_by_id:
                raise ValueError(
                    f"stage {s.id!r}: on_reject target {reject_to!r} is not a known stage id; "
                    f"have {sorted(idx_by_id)}"
                )
            if idx_by_id[reject_to] >= i:
                raise ValueError(
                    f"stage {s.id!r}: on_reject target {reject_to!r} must be STRICTLY UPSTREAM "
                    f"(target index {idx_by_id[reject_to]} >= rejecting stage index {i}) — "
                    f"a downstream/self target would push a rejected item toward the PR gate"
                )
            eff = getattr(s, "max_rounds", None) or rework_max_rounds
            if not (isinstance(eff, int) and eff > 0):
                raise ValueError(
                    f"stage {s.id!r}: declares on_reject but effective max_rounds is not a positive int "
                    f"(stage.max_rounds={getattr(s, 'max_rounds', None)!r}, rework.max_rounds={rework_max_rounds!r}); "
                    f"set rework.max_rounds (or the per-edge max_rounds) to a positive int"
                )


def _reject_unknown_keys(block: dict, valid: tuple, where: str) -> None:
    unknown = sorted(set(block) - set(valid))
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) {unknown}; valid keys: {sorted(valid)} — "
            f"an unrecognised knob here is never applied, and this loader has no other "
            f"way to tell you that"
        )


def validate_transparency(stages: list, record: dict, evidence: dict, criteria_dir=None) -> None:
    """Fail LOUD at load on a transparency knob that cannot mean what it says.

    Mirrors ``validate_chain``: names the offending value AND the valid set, never
    a silent fallback to a default. A typo'd ``scan: reduct`` that quietly falls
    back to "redact" reads as "redaction is on" while agent-authored text goes to
    a public repo unscanned.

    Checks:

    * ``record``/``evidence`` carry only known keys, and ``record.scan`` /
      ``record.events`` only known values — checked even when the block is
      disabled, because a typo is still a typo the moment someone flips it on;
    * no stage is an evidence source unless its ``run:`` is ``cmd:`` (an LLM stage
      would be reporting on its own work, and the dossier labels evidence as
      machine-captured) — the ``evidence.require_deterministic`` knob;
    * no stage names ``criteria:`` without a ``board.criteria_dir`` to resolve it
      against.

    Only facts the config can answer about ITSELF live here. Whether a criteria
    file EXISTS is a fact about the filesystem, like whether a board option
    exists — that belongs with the environmental checks at engine preflight
    (``preflight_reject_field``), not in a loader that legitimately runs where the
    target repo does not.
    """
    _reject_unknown_keys(record, _RECORD_KEYS, "record")
    _reject_unknown_keys(evidence, _EVIDENCE_KEYS, "evidence")

    scan = record.get("scan", "redact")
    if scan not in _RECORD_SCAN_MODES:
        raise ValueError(
            f"record.scan: {scan!r} is not a known scan mode; valid: {sorted(_RECORD_SCAN_MODES)}"
        )

    unknown_events = [e for e in (record.get("events") or _RECORD_EVENTS) if e not in _RECORD_EVENTS]
    if unknown_events:
        raise ValueError(
            f"record.events: unknown event(s) {sorted(unknown_events)}; valid: {sorted(_RECORD_EVENTS)}"
        )

    require_deterministic = bool(evidence.get("require_deterministic", True))
    for s in stages:
        if s.evidence and require_deterministic and not s.run.startswith("cmd:"):
            raise ValueError(
                f"stage {s.id!r}: evidence: true but run={s.run!r} is not deterministic — "
                f"only a cmd: stage may be an evidence source, because the engine reads that "
                f"stage's output from the pipe; an LLM stage would be reporting on its own work "
                f"while the dossier labels it machine-captured. Set "
                f"evidence.require_deterministic: false to allow it anyway"
            )
        if s.criteria and not criteria_dir:
            raise ValueError(
                f"stage {s.id!r}: criteria {s.criteria!r} is a filename relative to "
                f"board.criteria_dir, but no board.criteria_dir is set — the path resolves to "
                f"nothing and the record would cite a standard the engine never read"
            )


def load_pipeline_from_dict(data: dict) -> EngineConfig:
    pl = data.get("pipeline", data) or {}
    board = data.get("board") or {}
    stages = []
    for s in pl.get("stages", []):
        of = s.get("on_fail")
        of_dict = of if isinstance(of, dict) else {}
        oj = s.get("on_reject")
        oj_dict = oj if isinstance(oj, dict) else {}
        mr = oj_dict.get("max_rounds")
        stages.append(
            Stage(
                id=s.get("uses") or s.get("id") or s.get("name") or "",
                run=s.get("run", ""),
                on_success=_on_success(s.get("on_success")),
                gate=s.get("gate"),
                retry=int(of_dict.get("retry", s.get("retry", 0)) or 0),
                on_fail=(of_dict.get("then") or (of if isinstance(of, str) else None) or "park"),
                rewind_to=of_dict.get("rewind_to") or of_dict.get("to"),
                reject_to=(oj_dict.get("to") if oj_dict else None),
                max_rounds=(int(mr) if isinstance(mr, int) else None),
                on_exhausted=(oj_dict.get("on_exhausted") or "park") if oj_dict else "park",
                criteria=s.get("criteria"),
                evidence=bool(s.get("evidence", False)),
            )
        )
    rework = pl.get("rework") or {}
    record = pl.get("record") or {}
    evidence = pl.get("evidence") or {}
    validate_chain(stages, rework_max_rounds=rework.get("max_rounds"))
    validate_transparency(stages, record, evidence, criteria_dir=board.get("criteria_dir"))
    return EngineConfig(
        stages=stages,
        trigger_status=(pl.get("trigger") or {}).get("on_status", "Todo"),
        pr_status=pl.get("pr_status", "In Review"),
        done_status=pl.get("done_status", "Done"),
        concurrency=int(pl.get("concurrency", 1)),
        token_ceiling=(pl.get("budget") or {}).get("max_tokens"),
        project=pl.get("project", data.get("project", "unknown")),
        bounce_field=rework.get("bounce_field", "Bounces"),
        max_rounds=rework.get("max_rounds"),
        working_status=pl.get("working_status"),
        park_status=pl.get("park_status"),
        require_issue=bool(pl.get("require_issue", False)),
        record=RecordConfig(
            enabled=bool(record.get("enabled", False)),
            events=tuple(record.get("events") or _RECORD_EVENTS),
            sticky_marker=record.get("sticky_marker", "board-pilot:run"),
            templates_dir=record.get("templates_dir"),
            max_body_chars=int(record.get("max_body_chars", 60000)),
            scan=record.get("scan", "redact"),
        ),
        evidence=EvidenceConfig(
            dir=evidence.get("dir"),
            require_deterministic=bool(evidence.get("require_deterministic", True)),
        ),
    )


def load_pipeline_from_yaml(path) -> EngineConfig:
    import yaml  # lazy — only the real CLI needs PyYAML; tests build EngineConfig directly

    from pathlib import Path

    return load_pipeline_from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
