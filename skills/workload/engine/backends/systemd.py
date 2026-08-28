"""systemd user units: a .service plus a .timer, or a .path for a watcher.

systemd is the only backend here that promises all four things by itself, and
each promise is written into the unit rather than asserted in this docstring:

  deadline            RuntimeMaxSec=
  process group kill  KillMode=control-group, which ends the whole cgroup and
                      not merely the process that was started
  single flight       a timer does not trigger a service that is still active
  missing detection   the journal records every start and every exit with a
                      timestamp, so "it did not run" is a question with an
                      answer here. On launchd it is not: that run counter
                      resets and takes the evidence with it.

Because nothing is missing, no guard script is attached and every file of the
artifact is a unit file.
"""

from __future__ import annotations

from engine.model import (
    MARKER_ENV_DIGEST,
    MARKER_ENV_ID,
    Guarantee,
    Step,
    declaration_digest,
    ensure_id_safe,
    ensure_unit_safe,
)

from . import base, wrapper

NATIVE_GUARANTEES = frozenset({
    Guarantee.deadline,
    Guarantee.process_group_kill,
    Guarantee.single_flight,
    Guarantee.missing_detection,
})

#: systemd's own weekday names, in the numbering base.py uses (Sunday is 0).
_DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

#: The two variables ownership is read back out of. A declared variable of the
#: same name would rename who the live unit belongs to, so the marker is written
#: first and a collision is dropped here; the declaration gate refuses it by
#: name long before, so nothing is silently lost in practice.
_MARKER_NAMES = frozenset({MARKER_ENV_ID, MARKER_ENV_DIGEST})

_CONTINUOUS = frozenset({"daemon", "agent"})


class SystemdBackend:

    def __init__(self, unit_dir="<home>/.config/systemd/user",
                 label_prefix=base.DEFAULT_LABEL_PREFIX):
        self.name = "systemd"
        self.unit_dir = unit_dir
        self.label_prefix = label_prefix
        self.platforms = frozenset({"linux"})
        self.kinds = frozenset({"recurring", "interval", "daemon", "watch", "agent"})
        self.guarantees = NATIVE_GUARANTEES
        self.wrappable = True
        self.requires_elevation = False
        self.provisionable = True
        self.kind_remedy = ("a run that happens once is not a unit that "
                            "repeats; carry it with runtime dispatcher instead")

    # -- naming ------------------------------------------------------------

    def directory(self, ctx) -> str:
        return self.unit_dir.replace("<home>", str(ctx.home))

    def unit_ref(self, w, ctx=None) -> str:
        """`<id>.service`, or `<prefix>.<id>.service` where a declaration carries
        a prefix of its own.

        The same field as on launchd, for the same reason: a hand made unit is
        called what it is called, and `adopt` can only take it over if the
        declaration is able to build that name. Without the field the name is
        left unchanged, so that no existing unit is ever renamed. In both cases
        the id stays the last name segment before
        `.service`, worauf die Zuordnung beruht.
        """
        prefix = getattr(w.placement, "label_prefix", None)
        return f"{prefix}.{w.id}.service" if prefix else f"{w.id}.service"

    def unit_name(self, w, appointment=None) -> str:
        """See launchd.unit_name: the name the machine uses, asked once."""
        return self.unit_ref(w)

    # -- render ------------------------------------------------------------

    def render(self, w, h, ctx) -> base.Artifact:
        base.ensure_kind(self, w, self.kind_remedy)
        ensure_id_safe(w)
        digest = declaration_digest(w)
        supplied = wrapper.supplies(w, self)
        command = base.command_of(w)
        directory = self.directory(ctx)

        files = [base.RenderedFile(
            path=f"{directory}/{w.id}.service",
            mode=0o644,
            content=self._service(w, ctx, digest, command),
        )]
        kind = w.placement.kind
        if kind == "recurring":
            base.ensure_local_timezone(w, ctx, self)
            files.append(base.RenderedFile(
                path=f"{directory}/{w.id}.timer",
                mode=0o644,
                content=self._timer(w, digest, self._on_calendar(w)),
            ))
        elif kind == "interval":
            files.append(base.RenderedFile(
                path=f"{directory}/{w.id}.timer",
                mode=0o644,
                content=self._timer(w, digest, None),
            ))
        elif kind == "watch":
            files.append(base.RenderedFile(
                path=f"{directory}/{w.id}.path",
                mode=0o644,
                content=self._path_unit(w, digest),
            ))
            if getattr(w.schedule, "every_sec", None):
                # The cadence beside a watcher is a fallback, not a duplicate:
                # a path event can arrive before the file has finished arriving.
                files.append(base.RenderedFile(
                    path=f"{directory}/{w.id}.timer",
                    mode=0o644,
                    content=self._timer(w, digest, None),
                ))

        ordered = tuple(files)
        return base.Artifact(
            runtime=self.name,
            unit_ref=self.unit_ref(w, ctx),
            files=ordered,
            digest=base.digest_of(ordered),
            guarantees_native=self.guarantees,
            guarantees_wrapped=frozenset(supplied),
            notes="",
        )

    def _service(self, w, ctx, digest, command) -> str:
        marker = base.marker_env(w, digest)
        lines = [
            "[Unit]",
            _directive(w, "Description", w.display_title, "title"),
            f"X-BridgeWorkload={w.id}",
            f"X-BridgeWorkloadDigest={digest}",
            "",
            "[Service]",
            "Type=simple" if w.placement.kind in _CONTINUOUS else "Type=oneshot",
        ]
        if command:
            for index, argument in enumerate(command):
                ensure_unit_safe(argument, key_path=f"execution.command[{index}]",
                                 workload_id=w.id)
            lines.append(f"ExecStart={base.shell_command(command)}")
        working_dir = base.working_dir_of(w)
        if working_dir:
            lines.append(_directive(w, "WorkingDirectory", working_dir,
                                    "execution.working_dir"))
        lines.extend(_environment_lines(w, marker, base.env_of(w)))
        deadline = base.timeout_of(w)
        if deadline and w.placement.kind not in _CONTINUOUS:
            lines.append(f"RuntimeMaxSec={int(deadline)}")
        # Ending the control group, never only the process that was started:
        # a grandchild left holding the output pipe blocks the cleanup forever.
        lines.append("KillMode=control-group")
        lines.append("KillSignal=SIGTERM")
        if w.placement.kind in _CONTINUOUS:
            lines.append("Restart=always")
            lines += ["", "[Install]", "WantedBy=default.target"]
        return "\n".join(lines) + "\n"

    def _on_calendar(self, w) -> str:
        hour, minute, shift = base.start_of(w)
        days = base.weekdays_of(w, shift)
        prefix = ",".join(_DAY_NAMES[d] for d in days) + " " if days else ""
        return f"{prefix}*-*-* {hour:02d}:{minute:02d}:00"

    def _timer(self, w, digest, on_calendar) -> str:
        lines = [
            "[Unit]",
            _directive(w, "Description", f"Schedule of {w.display_title}", "title"),
            f"X-BridgeWorkload={w.id}",
            "",
            "[Timer]",
        ]
        if on_calendar:
            lines.append(f"OnCalendar={on_calendar}")
            # A missed appointment is not silently made up for later: that is a
            # decision for the operator, not a default.
            lines.append("Persistent=false")
        else:
            every = int(w.schedule.every_sec)
            lines.append(f"OnBootSec={every}")
            lines.append(f"OnUnitActiveSec={every}")
        lines.append(f"Unit={w.id}.service")
        lines += ["", "[Install]", "WantedBy=timers.target"]
        return "\n".join(lines) + "\n"

    def _path_unit(self, w, digest) -> str:
        lines = [
            "[Unit]",
            _directive(w, "Description", f"Watch of {w.display_title}", "title"),
            f"X-BridgeWorkload={w.id}",
            "",
            "[Path]",
        ]
        for index, path in enumerate(w.schedule.watch_paths):
            lines.append(_directive(w, "PathModified", path,
                                    f"schedule.watch_paths[{index}]"))
        lines.append(f"Unit={w.id}.service")
        lines += ["", "[Install]", "WantedBy=default.target"]
        return "\n".join(lines) + "\n"

    # -- step plans --------------------------------------------------------

    def _trigger_unit(self, a) -> str:
        """The unit that carries the schedule, which is the one to load."""
        for item in a.files:
            if str(item.path).endswith((".timer", ".path")):
                return str(item.path).rsplit("/", 1)[-1]
        return str(a.files[0].path).rsplit("/", 1)[-1]

    def install_steps(self, a, h) -> tuple:
        steps = [base.write_file_step(f) for f in a.files]
        steps.append(Step(argv=("systemctl", "--user", "daemon-reload"),
                          purpose="let systemd read the new unit files"))
        steps.append(Step(argv=("systemctl", "--user", "start", self._trigger_unit(a)),
                          purpose=f"start {self._trigger_unit(a)}"))
        return tuple(steps)

    def replace_steps(self, a, h) -> tuple:
        # stop, write, reload, start. Never a plain restart of the old unit: it
        # would keep running the bytes that are no longer declared.
        steps = [Step(argv=("systemctl", "--user", "stop", self._trigger_unit(a)),
                      purpose=f"stop {self._trigger_unit(a)} before it changes",
                      expect_rc=(0, 5))]
        steps.extend(base.write_file_step(f) for f in a.files)
        steps.append(Step(argv=("systemctl", "--user", "daemon-reload"),
                          purpose="let systemd read the changed unit files"))
        steps.append(Step(argv=("systemctl", "--user", "start", self._trigger_unit(a)),
                          purpose=f"start {self._trigger_unit(a)} again"))
        return tuple(steps)

    def disable_steps(self, a, h, reason: str) -> tuple:
        unit = self._trigger_unit(a)
        return (
            Step(argv=("systemctl", "--user", "stop", unit),
                 purpose=f"stop {unit} now, reason: {reason}", expect_rc=(0, 5)),
            Step(argv=("systemctl", "--user", "disable", unit),
                 purpose=f"keep {unit} off across reboots, reason: {reason}",
                 expect_rc=(0, 1)),
        )

    def uninstall_steps(self, a, h) -> tuple:
        paths = " ".join(base.shell_command([str(f.path)]) for f in a.files)
        return (
            Step(argv=("/bin/sh", "-c", f"rm -f {paths}"),
                 purpose=f"remove the unit files of {a.unit_ref}"),
            Step(argv=("systemctl", "--user", "daemon-reload"),
                 purpose="let systemd forget them"),
        )

    # -- reading the live source ------------------------------------------

    def default_probe(self, a, h) -> Step:
        return Step(
            argv=("systemctl", "--user", "show", a.unit_ref,
                  "-p", "Id,ActiveState,SubState,UnitFileState,FragmentPath,Environment"),
            purpose=f"ask systemd about {a.unit_ref}",
            expect_rc=(0, 3, 4),
        )

    def inspect_steps(self, a, h) -> tuple:
        return (self.default_probe(a, h),)

    def parse_inspection(self, outs, unit_ref: str = ""):
        text = _text_of(outs[0] if isinstance(outs, (list, tuple)) else outs)
        fields = dict(
            line.partition("=")[::2] for line in text.splitlines() if "=" in line
        )
        if not fields.get("Id"):
            return None
        from engine.reconcile import LiveUnit

        environment = fields.get("Environment", "")
        return LiveUnit(
            runtime=self.name,
            unit_ref=fields["Id"],
            path=fields.get("FragmentPath"),
            marker_id=_from_environment(environment, base.MARKER_ENV_ID),
            marker_observed=True,
            marker_digest=_from_environment(environment, base.MARKER_ENV_DIGEST),
            enabled=fields.get("UnitFileState") != "disabled",
            running=fields.get("ActiveState") in ("active", "activating"),
            raw=text,
        )

    def disabled_list_steps(self, a, h) -> tuple:
        # systemd keeps the off-list per unit rather than per domain, so this is
        # the same question asked in its idiom.
        return (
            Step(argv=("systemctl", "--user", "is-enabled", a.unit_ref),
                 purpose=f"read whether {a.unit_ref} is switched off persistently",
                 expect_rc=()),
        )

    def parse_disabled(self, outs, unit_ref: str):
        text = _text_of(outs[0] if isinstance(outs, (list, tuple)) else outs).strip()
        if not text:
            return None
        return text.splitlines()[0].strip() in ("disabled", "masked")

    def discover_steps(self, h) -> tuple:
        return (
            Step(argv=("systemctl", "--user", "list-timers", "--all", "--no-pager"),
                 purpose="enumerate the timers of the calling user"),
            Step(argv=("systemctl", "--user", "list-units", "--type=service",
                       "--all", "--no-pager", "--plain", "--no-legend"),
                 purpose="enumerate the services of the calling user"),
        )

    def parse_discovery(self, outs) -> list:
        from engine.reconcile import LiveUnit

        units = []
        seen = set()
        for out in outs or ():
            for line in _text_of(out).splitlines():
                for word in line.split():
                    if not word.endswith((".service", ".timer", ".path")):
                        continue
                    if word in seen:
                        continue
                    seen.add(word)
                    units.append(LiveUnit(
                        runtime=self.name, unit_ref=word, path=None,
                        marker_id=None, marker_digest=None,
                        enabled=True, running="active" in line or "running" in line,
                        raw=line,
                    ))
        return units


def _directive(w, key: str, value, key_path: str) -> str:
    """One `Key=Value` line, refusing a value that would not stay on it.

    A unit file is line based and nothing in it is escaped for us: a value
    carrying a newline does not arrive quoted in the next line, it arrives as
    the next DIRECTIVE. So the refusal happens here, where the line is built,
    rather than being hoped for upstream.
    """
    return f"{key}={ensure_unit_safe(value, key_path=key_path, workload_id=w.id)}"


def _quote_environment(w, name: str, value) -> str:
    """One assignment in the form systemd documents for a value with spaces.

    `Environment=` takes a SPACE SEPARATED list of assignments, so an unquoted
    `GREETING=hallo welt` sets GREETING to `hallo` and reads `welt` as a second
    assignment it cannot parse. The whole `NAME=value` is therefore wrapped in
    double quotes, which is systemd's own example form
    (`Environment="VAR1=word1 word2"`), and the two characters that would end
    the quoting early are escaped: a backslash and a double quote.
    """
    text = ensure_unit_safe(value, key_path=f"execution.env.{name}",
                            workload_id=w.id)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{name}={escaped}"'


def _environment_lines(w, marker, declared) -> list:
    """One `Environment=` line PER variable, each one fully quoted.

    Two reasons for one line each rather than one line for all of them. A value
    with a space cannot be told apart from the next assignment on a shared
    line, whatever the quoting; and a unit read back by a human shows one
    variable per line, which is where an unexpected one is actually noticed.

    The marker goes first and a declared variable of the same name is dropped:
    ownership is read back out of those two, so letting a declaration set one
    would not configure the run, it would rename who the live unit belongs to.
    The declaration gate refuses that by name, so this is the backstop under it.
    """
    lines = [_quote_environment(w, name, value) for name, value in marker.items()]
    for name in sorted(declared):
        if name in _MARKER_NAMES:
            continue
        lines.append(_quote_environment(w, name, declared[name]))
    return lines


def _text_of(out) -> str:
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    return getattr(out, "stdout", "") or ""


def _from_environment(environment: str, key: str):
    for assignment in environment.split():
        name, _, value = assignment.partition("=")
        if name == key:
            return value
    return None


def build(cfg) -> SystemdBackend:
    return SystemdBackend(label_prefix=getattr(cfg, "label_prefix",
                                               base.DEFAULT_LABEL_PREFIX))


SYSTEMD = build(None)
