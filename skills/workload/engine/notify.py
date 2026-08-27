"""The one adapter that knows how to speak, and the exit code is the fact.

The program is DECLARED, never named here. `workloads.notify_via` carries the
argv template of whatever an instance uses to reach a person: its path, its
flags, and the words that fill them. This module substitutes and executes, and
that is the whole of its knowledge.

It used to hold a filename, searched at two fixed paths, called with three
flags in a language this repository is not written in. The key that was supposed
to steer it was parsed, stored, serialised into the JSON output, and read by
nothing. So a fresh clone had an alarm path that could not exist, and an
instance that configured one was ignored.

The return code is LOAD BEARING. Zero means at least one channel really
delivered. Everything that dampens repeats hangs off that answer, so reading a
failure as a success buys silence and pays nothing for it.

The scar is in this repository twice. A watchdog reported into the void for
three months because its send function returned success while its channel had
been off since May. And the second guard on that machine still starts its 24
hour silence on the DECISION to notify rather than on a confirmed delivery, so a
total failure of both channels goes quiet for a day.

This module knows nothing about WHEN to speak or WHOM to tell. Recipients belong
to the configured program; a second place naming them would be a second list to
keep in step.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ConfigError


#: Distinguishes "caller said nothing" from "caller said: there is none".
_UNSET = object()


@dataclass(frozen=True)
class Sent:
    """What happened, never what was attempted."""

    delivered: bool
    reason: str = ""
    argv: tuple = ()


def _default_runner(argv, *, timeout_sec, **_):
    from engine import exec as exec_mod
    return exec_mod.run_argv(argv, timeout_sec=timeout_sec)


#: The words a template may ask for. Anything else stays as it stands, so a
#: literal brace in somebody's flag is not silently eaten.
PLACEHOLDERS = ("what", "where", "todo", "detail")


def argv_for(spec, *, what: str, where: str, todo: str, detail: str = "") -> list:
    """The program and its words, from the declared template. Pure.

    Substitution is PER ELEMENT and never on a joined string: a value with a
    space stays one argument. Joining and re-splitting would turn a two word
    host into two arguments, and the program would read the second as a flag.

    The `detail` segment is appended only when there is detail, because a flag
    whose value is empty is a flag with a missing value, and most programs read
    the next argument instead.
    """
    values = {"what": str(what), "where": str(where), "todo": str(todo),
              "detail": str(detail)}

    def fill(element) -> str:
        text = str(element)
        for name in PLACEHOLDERS:
            text = text.replace("{" + name + "}", values[name])
        return text

    argv = [fill(part) for part in (spec or {}).get("command", ())]
    if values["detail"]:
        argv += [fill(part) for part in (spec or {}).get("detail", ())]
    return argv


def notifier_spec(cfg):
    """The declared alarm path, or None when an instance has not declared one.

    None is an ANSWER. Inventing a filename here is how a skill acquires a
    dependency its own repository does not ship, which is exactly what the
    previous shape did.
    """
    raw = getattr(cfg, "notify_via", None)
    if not raw:
        return None
    if not isinstance(raw, Mapping):
        raise ConfigError(
            "workloads.notify_via: expected a mapping with a `command` list, "
            f"found {type(raw).__name__}", source="bridge-config.yaml")
    command = raw.get("command")
    if not isinstance(command, (list, tuple)) or not command:
        raise ConfigError(
            "workloads.notify_via.command: expected a non empty list, the "
            "program first and then its flags", source="bridge-config.yaml")
    detail = raw.get("detail") or ()
    if not isinstance(detail, (list, tuple)):
        raise ConfigError(
            "workloads.notify_via.detail: expected a list appended only when "
            "there is detail to send", source="bridge-config.yaml")
    return {"command": [_expanded(part) for part in command],
            "detail": [_expanded(part) for part in detail]}


def _expanded(element) -> str:
    """A leading `~/` becomes the home directory, and nothing else changes.

    The argv is handed to a process and never to a shell, so a tilde would
    arrive as a literal directory name and the program would be reported
    missing. Done here, at the boundary that reads the configuration, because
    `argv_for` is pure and stays that way.
    """
    text = str(element)
    return os.path.expanduser(text) if text.startswith("~/") else text


def send(*, argv, runner=None, timeout_sec: int = 60) -> Sent:
    """Run the declared program with the words it was given. Never raises.

    The caller is a watchdog, so it may not be taken down by the thing it uses
    to complain: every failure comes back as an answer.
    """
    argv = [str(a) for a in (argv or ())]
    if not argv:
        return Sent(delivered=False, reason="no notifier configured", argv=())
    run = runner or _default_runner
    try:
        done = run(argv, timeout_sec=timeout_sec)
    except Exception as exc:                      # noqa: BLE001 - see docstring
        return Sent(False, f"{type(exc).__name__}: {exc}", tuple(argv))

    rc = getattr(done, "rc", None)
    if rc is None:
        rc = getattr(done, "returncode", None)
    if rc == 0:
        return Sent(True, "", tuple(argv))
    # Named, because a caller writing this into a state file wants to know
    # WHICH failure it was on the next run.
    said = (getattr(done, "stderr", "") or getattr(done, "stdout", "") or "").strip()
    tail = said.splitlines()[-1] if said else ""
    return Sent(False, f"exit {rc}{': ' + tail if tail else ''}", tuple(argv))


# ── which verdict wakes somebody, and on whose say-so ────────────────────────
#
# Four sets, written out rather than derived from severity. Severity measures
# attention while READING a report; this measures whether a phone should ring
# at three in the morning, and the two are different questions about the same
# verdict. Written out, growing the enum breaks a test instead of silently
# adding a state that either shouts or disappears.

#: The run happened and went wrong.
WAKES_ON_FAILURE = None
#: The run did not happen at all.
WAKES_ON_MISSING = None
#: Louder than the declaration, and sent WITHOUT asking `notify_on`.
WAKES_ALWAYS = None
#: Not trouble, or trouble a person handles at a keyboard rather than in bed.
WAKES_NOBODY = None


def _sets():
    from engine import model
    st = model.WorkloadState
    failure = {
        st.last_run_failed,   # the trace says so in the run's own words
        st.stopped,           # a thing that should be running fell over. Not
                              # `missing`: it did not skip an appointment, it
                              # went down.
    }
    missing = {
        st.overdue,           # its appointment passed and no line was written
        st.absent,            # the strongest form of "it did not come": the
                              # unit is not on the machine at all. It shares
                              # the channel with overdue without ever firing
                              # alongside it, because reconcile excludes trace
                              # findings for absent outright.
    }
    always = {
        st.retired_but_live,  # the loudest thing this skill says, and possibly
                              # a security incident. Nobody edits notify_on on
                              # the way out while retiring a run, so an opt-in
                              # gate here is a gate the retiring hand never
                              # touched.
        st.grant_orphaned,    # the run ends rc=0 and is simply shown nothing,
                              # so no trace can ever carry it. It is already
                              # opt-in through another field: it exists only
                              # where `placement.privacy_grants` is declared.
    }
    nobody = set(st) - failure - missing - always
    return failure, missing, always, nobody


WAKES_ON_FAILURE, WAKES_ON_MISSING, WAKES_ALWAYS, WAKES_NOBODY = _sets()

#: What each bucket is called in `response.notify_on`. `integrity` has no word
#: in that vocabulary on purpose, and that is exactly why it does not ask.
_ASKED_AS = {"failure": "failure", "missing": "missing"}


def route(finding, workload) -> str | None:
    """Which bucket this verdict belongs to, or None if nobody is woken.

    `unknown` is in WAKES_NOBODY and never routes. The reconcile driving this
    reaches its hosts over ssh from a laptop, so a closed lid produces
    `unknown` for every run on every host at once; seventeen jobs were once
    wrongly called overdue by exactly that collapse.
    """
    state = getattr(finding, "state", None)
    if state in WAKES_ALWAYS:
        return "integrity"
    for bucket, word in _ASKED_AS.items():
        if state in (WAKES_ON_FAILURE if bucket == "failure" else WAKES_ON_MISSING):
            response = getattr(workload, "response", None)
            asked = tuple(getattr(response, "notify_on", ()) or ()) if response else ()
            return bucket if word in asked else None
    return None


# ── dampening: what stops the same thing being said every half hour ──────────

#: Wall clock silence per key. Four hours and not watchdog.sh's twenty four:
#: that number is calibrated for nine keepalive services on a fifteen minute
#: tick with ONE key per service. Here the key is already per appointment, so
#: the same number would turn a whole day's outage of a five minute run into a
#: single message. To be re-measured after the first weeks in service.
BACKOFF_HOURS = 4

#: Messages that actually ARRIVED, per day, across everything. Six and not
#: eight, because a phone is more easily worn out than a dashboard.
DAILY_CAP = 6

#: Only `stopped` waits for a second look. A line already written to disk does
#: not become truer by being read twice, and `overdue` carries its grace
#: upstream in reconcile. `stopped` reads a live measurement that flickers
#: around a restart, which is exactly the case watchdog.sh's DEBOUNCE_RUNS=2
#: was written for.
PASSES_BEFORE_ALARM = {"stopped": 2}

_ORDER = {"integrity": 0, "failure": 1, "missing": 2}


@dataclass(frozen=True)
class Dispatched:
    """What this pass did. Never what it meant to do."""

    sent: int = 0
    suppressed: int = 0
    note: str = ""


def fingerprint(finding) -> str:
    """What makes this the SAME trouble as last time, or a new one.

    It is the verdict's own sentence, hashed. Not the timestamp parsed back out
    of it: the sentence already contains the trace stamp for a failed run and
    the computed due moment for a missing one, so parsing would be a second
    derivation of a value the first one already carries, and this skill has
    lost runs twice to exactly that pair.

    For a verdict with no anchor in it (a retired unit still live, a moved
    grant) the sentence is constant, so the fingerprint is constant and the
    wall clock alone decides. That is the intended behaviour, not a gap.

    The cost is honest and small: rewording a sentence makes one alarm read as
    new, once.
    """
    import hashlib
    return hashlib.sha256(str(getattr(finding, "detail", "")).encode("utf-8")).hexdigest()[:16]


def _key(finding, bucket) -> str:
    return f"{finding.workload_id}|{getattr(finding, 'appointment', '') or ''}|{bucket}"


def _load_state(path):
    """The remembered state, and whether it survived. Never raises."""
    import json
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"keys": {}, "day": "", "delivered_today": 0}, ""
    except Exception as exc:                       # noqa: BLE001
        # A torn write resets every streak and backoff at once, so the next
        # pass re-announces everything already settled. That storm has to be
        # readable AS a lost state and not as a real multi-incident.
        return ({"keys": {}, "day": "", "delivered_today": 0},
                f"previous notify state was unreadable ({type(exc).__name__}), "
                f"counters start again")
    if not isinstance(raw, dict):
        return ({"keys": {}, "day": "", "delivered_today": 0},
                "previous notify state was not an object, counters start again")
    raw.setdefault("keys", {})
    raw.setdefault("day", "")
    raw.setdefault("delivered_today", 0)
    return raw, ""


def _save_state(path, state) -> None:
    """Written through a temporary file, like both existing watchdogs.

    A target path written in place and caught mid-write by a reboot leaves
    exactly the broken file `_load_state` has to recover from, without anybody
    having made that happen on purpose.
    """
    import json
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def compose(entries, *, capped: bool = False):
    """One message out of everything this pass found. Pure.

    Bundled rather than one message per finding: several failures in one pass
    usually share one cause, and N messages burn N of the day's places for one
    incident. Each LINE still names its own run and appointment, so bundling
    costs no detail.
    """
    first = entries[0]
    finding, workload, _bucket = first
    title = str(getattr(workload, "title", "") or workload.id)
    appointment = getattr(finding, "appointment", "") or ""
    what = f"{title} ({appointment})" if appointment else title
    if len(entries) > 1:
        what = f"{what} and {len(entries) - 1} more"
    host = str(getattr(getattr(workload, "placement", None), "host", "") or "")
    where = host or "unknown host"
    # The repair sentence is the FINDING's own. reconcile keeps a hand written
    # one per verdict; a second copy maintained here would drift, and then the
    # message carries last year's instructions while the report shows this
    # year's.
    todo = str(getattr(finding, "hint", "") or "") or "no repair is recorded for this verdict"
    lines = []
    for one, w, bucket in entries:
        name = getattr(one, "appointment", "") or ""
        who = f"{w.id}.{name}" if name else str(w.id)
        lines.append(f"[{bucket}] {who}: {one.detail}")
    if capped:
        lines.append("further findings are suppressed for the rest of today; "
                     "the full picture stays on the workloads page")
    return what, where, todo, "\n".join(lines)


def _sender_for(spec):
    def send_one(*, what, where, todo, detail):
        return send(argv=argv_for(spec, what=what, where=where, todo=todo,
                                  detail=detail))
    return send_one


def dispatch(report, workloads, *, state_path, now, cfg=None, sender=None,
             cap: int = DAILY_CAP, backoff_hours: int = BACKOFF_HOURS) -> Dispatched:
    """One pass: route, dampen, say it once, and remember only what arrived."""
    by_id = {w.id: w for w in (workloads or ())}
    state, note = _load_state(state_path)

    if sender is None:
        spec = notifier_spec(cfg)
        if spec is None:
            # Being unable to speak is itself the one thing this layer may never
            # be quiet about, so it comes back as a visible note that names the
            # key a reader has to set, rather than as an exception nobody
            # catches or a filename nobody chose.
            note = (note + '; ' if note else '') + (
                'no alarm path: workloads.notify_via is not configured, so '
                'nothing can be said')
            _save_state(state_path, state)
            return Dispatched(sent=0, suppressed=0, note=note)
        sender = _sender_for(spec)

    day = now.date().isoformat()
    if state.get("day") != day:
        state["day"] = day
        state["delivered_today"] = 0

    findings = list(getattr(report, "findings", ()) or ())
    # A host that did not answer is not news AND not recovery. Its runs keep
    # their remembered state untouched: counted as recovery, a flaky ssh hop
    # would tear a real ongoing incident in half and announce its second half
    # as new.
    unreachable = {f.workload_id for f in findings
                   if getattr(f, "state", None) in WAKES_NOBODY
                   and getattr(f, "state", None) is _unknown()}

    seen, candidates, suppressed = set(), [], 0
    for finding in findings:
        workload = by_id.get(getattr(finding, "workload_id", None))
        if workload is None:
            continue
        bucket = route(finding, workload)
        if bucket is None:
            continue
        key = _key(finding, bucket)
        seen.add(key)
        entry = dict(state["keys"].get(key) or {})

        needed = PASSES_BEFORE_ALARM.get(getattr(finding.state, "value", ""), 1)
        streak = int(entry.get("streak") or 0) + 1
        entry["streak"] = streak
        state["keys"][key] = entry
        if streak < needed:
            suppressed += 1
            continue

        mark = fingerprint(finding)
        if entry.get("fingerprint") == mark and _within(entry.get("last_alert_at"),
                                                        now, backoff_hours):
            suppressed += 1
            continue
        candidates.append((finding, workload, bucket, key, mark))

    # Everything that had a key and no longer has a finding recovered, unless
    # its host simply could not be asked this time.
    for key in list(state["keys"]):
        if key in seen:
            continue
        if key.split("|", 1)[0] in unreachable:
            continue
        state["keys"].pop(key, None)

    sent = 0
    if candidates:
        candidates.sort(key=lambda c: _ORDER.get(c[2], 9))
        room = cap - int(state.get("delivered_today") or 0)
        if room <= 0:
            suppressed += len(candidates)
            candidates = []
        else:
            capped = int(state.get("delivered_today") or 0) + 1 >= cap
            what, where, todo, detail = compose(
                [(f, w, b) for f, w, b, _k, _m in candidates], capped=capped)
            answer = sender(what=what, where=where, todo=todo, detail=detail)
            if getattr(answer, "delivered", False):
                sent = 1
                state["delivered_today"] = int(state.get("delivered_today") or 0) + 1
                stamp = now.isoformat()
                # ONLY after a confirmed delivery. The whole point.
                for _f, _w, _b, key, mark in candidates:
                    state["keys"][key] = {"fingerprint": mark, "last_alert_at": stamp,
                                          "streak": state["keys"][key].get("streak", 1)}
            else:
                note = (note + "; " if note else "") + f"send failed: {answer.reason}"

    _save_state(state_path, state)
    return Dispatched(sent=sent, suppressed=suppressed, note=note)


def _unknown():
    from engine import model
    return model.WorkloadState.unknown


def _within(stamp, now, hours) -> bool:
    from datetime import datetime, timedelta
    if not stamp:
        return False
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    return now - then < timedelta(hours=hours)
