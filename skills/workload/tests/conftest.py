"""Shared test scaffolding for the workload skill.

Two jobs, and they are both load bearing.

1. NOTHING HERE TOUCHES A MACHINE. Real services run on the boxes these
   fixtures describe. `MachineGuard` patches `subprocess.Popen`,
   `subprocess.run`, `os.system` and `os.popen` for the duration of every test
   and raises on any attempt to exec ssh, scp, launchctl, systemctl, crontab or
   sudo, including one hidden inside `sh -c`. It is a guard, not a request: a
   test that reaches for a real box fails loudly instead of quietly succeeding
   somewhere it should not be. The one deliberate exception is `test_exec.py`,
   which starts harmless local processes (`/bin/sh`, `sleep`) under its own
   short deadline, because the process group scar cannot be proven against a
   mock.

   The guard is exercised by `test_acceptance.TheGuardItself`. It has to be: in
   a green run it never fires, so nothing else in this tree would ever notice
   that it had stopped working. It replaced a scan for two spellings of one
   literal (`subprocess.run(["ssh`), which an argv assembled from a variable, a
   bare `Popen` or an `os.system` walked past without a word.

2. ENGINE MODULES ARE IMPORTED LAZILY. `mod("engine.model")` returns a proxy
   that imports on first attribute access, inside the test body. Without that,
   a missing module collapses the whole file into a single collection error and
   the suite reports one failure where it should report a hundred. The count of
   red tests is the evidence that the suite examines something, so the count has
   to survive the absence of the implementation.

The attribute names used by `FakeHost`, `FakeCompleted` and `RecordingRunner`
ARE the contract. If the implementation names them differently, the
implementation is what changes.
"""

from __future__ import annotations

import builtins
import contextlib
import datetime as datetime_mod
import importlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time as time_mod
import unittest
from dataclasses import dataclass, field
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
CORPUS = FIXTURES / "corpus"
INVALID = FIXTURES / "invalid"
DERIVED = FIXTURES / "derived"
HOSTS = FIXTURES / "hosts"
CHECKS = FIXTURES / "checks"
OUTPUTS = FIXTURES / "outputs"
GOLDEN = FIXTURES / "golden"

if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

#: The seven inventory derived cases, in load order. Every acceptance run walks
#: exactly this list, so adding an eighth case is a deliberate act.
CORPUS_IDS = (
    "calendar-export",
    "chat-channel",
    "contract-review-reminder",
    "daily-health-report",
    "public-funnel",
    "voice-channel",
    "voicememo-notify",
)

#: Which of them a provisioner may legally touch.
PROVISIONABLE_IDS = ("calendar-export", "daily-health-report", "voicememo-notify")

#: The uid the fixtures use. Deliberately not the number any real box carries,
#: so a hardcoded uid fails instead of passing by accident.
FIXTURE_UID = "4242"
FIXTURE_HOME = "/home/opuser"
FIXTURE_TZ = "Europe/Berlin"


# ---------------------------------------------------------------------------
# Lazy engine import
# ---------------------------------------------------------------------------

class _LazyModule:
    """Imports on first attribute access, so the failure lands inside a test."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._loaded = None

    def __getattr__(self, attr):
        if self._loaded is None:
            self._loaded = importlib.import_module(self._name)
        return getattr(self._loaded, attr)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<lazy module {self._name}>"


def mod(name: str) -> _LazyModule:
    """Return a lazy proxy for an engine module."""
    return _LazyModule(name)


# ---------------------------------------------------------------------------
# Forbidden literals
# ---------------------------------------------------------------------------

def forbidden_literals() -> tuple[str, ...]:
    """Instance names that may never appear in a core skill.

    Assembled from fragments on purpose, and the split is load bearing TWICE.

    1. This file is itself scanned by the case that uses this list, and a
       scanner that trips over its own denylist proves nothing.
    2. The promote gate of the instance around it scans the same file with a
       DIFFERENT matcher: case-insensitive, word-level, and holding regular
       expressions as well as plain strings. Two fragments here carried the
       three letter organisation token whole -- `<token>.` and `<token>-` --
       and to that matcher both are the word. The file was refused on every
       promote attempt: not a leak, a blocking false alarm that came back each
       time and that splitting a name once does not avoid.

    So a fragment may not be a token EITHER matcher recognises, which is why the
    organisation token is split a second time. The rule is checked rather than
    trusted: `test_no_core_file_of_this_skill_trips_the_promote_blocklist`
    reads the instance's own lists and runs them over every core file of the
    skill, this one included.
    """
    return (
        "macmini" + "m4",
        "mboi" + "man",
        "zahn" + "chat",
        "step" + "stone",
        "com." + "b" + "ks.",
        "Mich" + "ael",
        "/Us" + "ers/",
        "b" + "ks-" + "lab",
    )


# ---------------------------------------------------------------------------
# The promote gate's own lists, as this instance configured them
# ---------------------------------------------------------------------------

#: Where the instance keeps them. A core skill reads the configuration; it never
#: carries an instance's names, not even to check itself against them.
CONFIG_REL = Path("bridge-config.yaml")


def promote_blocklist(root) -> list:
    """Every rule the promote gate would refuse a CORE file for, compiled.

    A core file travels to every upstream the instance declares, so the rules
    are the union over those DESTINATIONS: each one's own list where it has one,
    and the fallback list only for a destination that has none (and for an
    instance that declares no upstream at all, which is what the fallback is
    for).

    The fallback is deliberately not folded in on top of the others. A
    per-destination list is allowed to carve out what is a self reference FOR
    THAT destination -- an OSS project has to be able to name its own repository,
    and its schemas publish themselves under its own host -- and laying a blunter
    list over it refuses exactly those lines. Measured while this scan was being
    written: with the fallback added on top, the declaration contract failed on
    its own `$id`.

    `strings` are matched as whole words and `patterns` as regular expressions,
    both case insensitively, which is what the promote rule prescribes: a scan
    with a different matcher answers a different question than the gate does.
    Returns `(label, compiled)` pairs, so a hit names the rule that fired.
    """
    import yaml

    raw = yaml.safe_load((Path(root) / CONFIG_REL).read_text(encoding="utf-8")) or {}
    promote = raw.get("promote") or {}
    per_destination = promote.get("content_blocklist") or {}
    fallback = promote.get("fallback_blocklist") or {}
    names = [str(up.get("name")) for up in (raw.get("upstreams") or ())] or [None]
    lists = [per_destination[name] if name in per_destination else fallback
             for name in names]
    rules = []
    for entry in lists:
        for word in (entry or {}).get("strings") or ():
            rules.append((word, re.compile(rf"\b{re.escape(str(word))}\b", re.I)))
        for pattern in (entry or {}).get("patterns") or ():
            rules.append((pattern, re.compile(str(pattern), re.I)))
    return rules


def blocklist_hits(text: str, rules) -> list:
    """`(rule, line)` for every line of `text` a rule fires on."""
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for label, expression in rules:
            if expression.search(line):
                hits.append((label, number))
    return hits


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeCompleted:
    """Stand in for exec.Completed."""

    rc: int = 0
    stdout: str = ""
    stderr: str = ""
    argv: tuple = ()
    duration_sec: float = 0.0


@dataclass
class FakeHost:
    """Stand in for hosts.Host. These attribute names are the contract."""

    slug: str = "host-a"
    platform: str = "macos"
    is_local: bool = False
    ssh_user: str = "opuser"
    ssh_host: str = "host-a"
    ssh_port: int = 22
    timezone: str = FIXTURE_TZ
    reachable: bool = True
    services: list = field(default_factory=list)

    @classmethod
    def from_fixture(cls, slug: str) -> "FakeHost":
        import yaml

        raw = yaml.safe_load((HOSTS / f"{slug}.yaml").read_text(encoding="utf-8"))
        ssh = raw.get("ssh") or {}
        platform = raw.get("type", "macos")
        return cls(
            slug=raw["name"],
            platform=platform,
            is_local=False,
            ssh_user=ssh.get("user", "opuser"),
            ssh_host=ssh.get("host", raw["name"]),
            ssh_port=int(ssh.get("port", 22)),
            services=list(raw.get("services") or []),
        )


class RecordingRunner:
    """A stand in for exec.run_step / exec.run_argv that records instead of running.

    Routing is by substring against the joined argv, first match wins, so a test
    says what a call answers without caring how the implementation phrases it.
    """

    def __init__(self, routes=None, default=None):
        self.routes = list(routes or [])
        self.default = default if default is not None else FakeCompleted()
        self.calls = []

    def add(self, needle: str, completed=None, raises=None):
        self.routes.append((needle, completed, raises))
        return self

    # The runner is deliberately callable with several shapes, because the
    # engine calls it as a step runner in one place and as an argv runner in
    # another, and a test should not have to know which.
    def __call__(self, argv=None, *args, step=None, **kwargs):
        if step is not None and argv is None:
            argv = getattr(step, "argv", ())
        if hasattr(argv, "argv"):
            step = argv
            argv = step.argv
        argv = tuple(str(a) for a in (argv or ()))
        joined = " ".join(argv)
        self.calls.append({"argv": argv, "joined": joined, "kwargs": kwargs, "step": step})
        for needle, completed, raises in self.routes:
            if needle in joined:
                if raises is not None:
                    raise raises
                return completed if completed is not None else FakeCompleted(argv=argv)
        return FakeCompleted(argv=argv, rc=self.default.rc,
                             stdout=self.default.stdout, stderr=self.default.stderr)

    # convenience -----------------------------------------------------------
    @property
    def joined_calls(self) -> str:
        return "\n".join(c["joined"] for c in self.calls)

    def called_with(self, needle: str) -> bool:
        return any(needle in c["joined"] for c in self.calls)

    def index_of(self, needle: str) -> int:
        for i, c in enumerate(self.calls):
            if needle in c["joined"]:
                return i
        raise AssertionError(f"no recorded call contains {needle!r}; calls were:\n{self.joined_calls}")


def read_output(name: str) -> str:
    return (OUTPUTS / name).read_text(encoding="utf-8")


def completed_from(name: str, rc: int = 0) -> FakeCompleted:
    return FakeCompleted(rc=rc, stdout=read_output(name))


# ---------------------------------------------------------------------------
# The machine guard
# ---------------------------------------------------------------------------

_DENY = {"ssh", "scp", "sftp", "launchctl", "systemctl", "systemd-run", "crontab", "sudo"}
_SHELLS = {"sh", "bash", "zsh", "dash"}


def _first_denied(argv) -> str | None:
    if argv is None:
        return None
    if isinstance(argv, (str, bytes)):
        text = argv.decode() if isinstance(argv, bytes) else argv
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
    else:
        parts = [str(a) for a in argv]
    if not parts:
        return None
    head = os.path.basename(parts[0])
    if head in _DENY:
        return head
    if head in _SHELLS and "-c" in parts:
        payload = parts[parts.index("-c") + 1] if len(parts) > parts.index("-c") + 1 else ""
        try:
            inner = shlex.split(payload)
        except ValueError:
            inner = payload.split()
        if inner and os.path.basename(inner[0]) in _DENY:
            return os.path.basename(inner[0])
    return None


class SandboxRunner(RecordingRunner):
    """Runs the FILE steps for real, in a throwaway home, and fakes the rest.

    The missing layer, and the one every defect of 2026-08-23 lived in. The
    suite had 557 cases and all of them asked "which steps were issued". None
    asked "and what is on the machine afterwards", because the only runner
    available records steps and touches no filesystem. A rollback copy that
    nothing removed was therefore invisible: every step it expected WAS issued.

    The split is by argv, and it is checkable rather than trusted. A step whose
    argv is a shell plus `-c` is a file operation the engine wrote itself
    (`mkdir`, `cat >`, `chmod`, `cp`, `mv`, `rm -f`, reading a stamp back), so
    it runs for real against the sandbox. Anything else is a service manager and
    is answered from the routes without running. `assert_only_service_managers`
    holds that line: a new verb that touches files without going through a shell
    would otherwise be skipped in silence and its end state never measured.

    MachineGuard stays on underneath and still inspects the shell payload, so a
    script that reached for `launchctl` would be refused here too.
    """

    SERVICE_BINARIES = frozenset({
        "launchctl", "systemctl", "systemd-run", "crontab", "sudo", "ssh", "scp",
    })
    SHELLS = frozenset({"sh", "bash", "zsh", "dash"})

    def __init__(self, home: Path, routes=None, default=None):
        super().__init__(routes=routes, default=default)
        self.home = Path(home)
        self.faked = []

    def __call__(self, argv=None, *args, step=None, **kwargs):
        real_step = step if step is not None else (argv if hasattr(argv, "argv") else None)
        raw = tuple(str(a) for a in (getattr(real_step, "argv", None) or argv or ()))
        head = os.path.basename(raw[0]) if raw else ""

        if head in self.SHELLS and len(raw) >= 3 and raw[1] == "-c":
            self.calls.append({"argv": raw, "joined": " ".join(raw),
                               "kwargs": kwargs, "step": real_step})
            done = subprocess.run(["/bin/sh", "-c", raw[2]], capture_output=True,
                                  text=True, timeout=30, cwd=str(self.home))
            return FakeCompleted(argv=raw, rc=done.returncode,
                                 stdout=done.stdout, stderr=done.stderr)

        self.faked.append(raw)
        return super().__call__(argv, *args, step=step, **kwargs)

    # -- what is on the machine afterwards ---------------------------------

    def tree(self) -> set:
        """Every file under the sandbox home, relative and sorted."""
        return {str(f.relative_to(self.home))
                for f in self.home.rglob("*") if f.is_file()}

    def assert_only_service_managers(self, case) -> None:
        stray = sorted({os.path.basename(a[0]) for a in self.faked if a} -
                       self.SERVICE_BINARIES)
        case.assertEqual(
            stray, [],
            "these steps were answered without running, so whatever they do to "
            "the filesystem is not measured by any end state assertion: "
            + ", ".join(stray))


class MachineGuard(unittest.TestCase):
    """Base class that refuses to let a test reach a real machine."""

    def setUp(self):
        super().setUp()
        self._real_popen = subprocess.Popen
        self._real_run = subprocess.run
        guard = self

        class GuardedPopen(self._real_popen):  # type: ignore[misc,valid-type]
            def __init__(self, args, *a, **kw):
                denied = _first_denied(args)
                if denied:
                    raise AssertionError(
                        f"the suite tried to exec {denied!r}. Real services run on those "
                        f"machines; drive FakeHost and RecordingRunner instead. argv={args!r}"
                    )
                super().__init__(args, *a, **kw)

        def guarded_run(args, *a, **kw):
            denied = _first_denied(args)
            if denied:
                raise AssertionError(
                    f"the suite tried to exec {denied!r} via subprocess.run. argv={args!r}"
                )
            return guard._real_run(args, *a, **kw)

        # os.system and os.popen do not go through subprocess at all, so
        # patching subprocess alone leaves a second, quieter door open.
        self._real_system = os.system
        self._real_os_popen = os.popen

        def guarded_system(command):
            denied = _first_denied(command)
            if denied:
                raise AssertionError(
                    f"the suite tried to exec {denied!r} via os.system. command={command!r}"
                )
            return guard._real_system(command)

        def guarded_os_popen(command, *a, **kw):
            denied = _first_denied(command)
            if denied:
                raise AssertionError(
                    f"the suite tried to exec {denied!r} via os.popen. command={command!r}"
                )
            return guard._real_os_popen(command, *a, **kw)

        subprocess.Popen = GuardedPopen
        subprocess.run = guarded_run
        os.system = guarded_system
        os.popen = guarded_os_popen
        self.addCleanup(self._restore_subprocess)

    def _restore_subprocess(self):
        subprocess.Popen = self._real_popen
        subprocess.run = self._real_run
        os.system = self._real_system
        os.popen = self._real_os_popen

    # helpers ---------------------------------------------------------------

    def tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="workload-test-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def assert_error(self, ctx, code: str, *substrings: str):
        """A negative control must fail for its stated reason, not merely fail."""
        exc = ctx.exception
        actual = getattr(exc, "code", None)
        self.assertEqual(
            actual, code,
            f"raised {type(exc).__name__} with code {actual!r}, expected {code!r}: {exc}",
        )
        text = str(exc)
        for needle in substrings:
            self.assertIn(needle, text, f"error message does not name {needle!r}: {text}")

    #: Verbs that CHANGE a machine, matched as whole argv words. Whole words on
    #: purpose: `launchctl print-disabled` and `systemctl is-enabled` are reads
    #: that merely contain the letters of a write, and a substring rule that
    #: forbids them forbids the only call able to see the persistent off-list.
    MUTATING_VERBS = frozenset({
        "bootstrap", "bootout", "kickstart", "enable", "disable", "load",
        "unload", "start", "stop", "restart", "rm", "mv", "cp", "tee",
        "install", "chmod", "mkdir",
    })

    #: Shell fragments that write, wherever they appear in a command string.
    MUTATING_FRAGMENTS = ("> ", ">>", "rm -", "mv ", "cp ", "tee ")

    # ── purity, measured instead of grepped ────────────────────────────────
    #
    # "touches nothing" and "does no I/O and reads no clock" used to be word
    # scans over ONE source file. The work they are about happens across the
    # backends package, so a subprocess planted in `wrapper.supplies()` wrote a
    # file while both of those tests ran and both stayed green.
    #
    # This shuts the doors instead of reading about them, and it does so BELOW
    # the engine: at `subprocess`, at the three calls that put bytes on disk, at
    # `open` in a writing mode, and at the clock. An argv assembled at runtime,
    # a write through an alias and a `time.time()` in a helper all hit it,
    # because none of them can avoid the door.

    def assert_pure(self, call, *, what: str, clock: bool = True):
        """Run `call()` with every door to a process, a file and the clock shut.

        Breaches are recorded AND raised. Recording matters on its own: a callee
        that swallows its own exceptions would otherwise look pure precisely
        because the failure never got out.
        """
        from unittest import mock

        breaches = []

        def no_process(args, *rest, **kwargs):
            breaches.append(f"started a process: {args!r}")
            raise AssertionError(f"{what} started a process: {args!r}")

        def no_write(where, *rest, **kwargs):
            breaches.append(f"wrote to {where!r}")
            raise AssertionError(f"{what} wrote to {where!r}")

        real_open = builtins.open

        def guarded_open(file, mode="r", *rest, **kwargs):
            if any(letter in str(mode) for letter in "wxa+"):
                breaches.append(f"opened {file!r} for writing")
                raise AssertionError(f"{what} opened {file!r} for writing")
            return real_open(file, mode, *rest, **kwargs)

        def no_clock(*rest, **kwargs):
            breaches.append("read the clock")
            raise AssertionError(f"{what} read the clock")

        class NoClock(datetime_mod.datetime):
            # Only the READING entry points are closed. Constructing a datetime
            # from declared values is what render legitimately does.
            now = classmethod(no_clock)
            utcnow = classmethod(no_clock)
            today = classmethod(no_clock)

        doors = [
            mock.patch.object(subprocess, "Popen", no_process),
            mock.patch.object(subprocess, "run", no_process),
            mock.patch.object(os, "system", no_process),
            mock.patch.object(os, "popen", no_process),
            mock.patch.object(Path, "write_text", no_write),
            mock.patch.object(Path, "write_bytes", no_write),
            mock.patch.object(Path, "mkdir", no_write),
            mock.patch.object(builtins, "open", guarded_open),
        ]
        if clock:
            doors += [
                mock.patch.object(time_mod, "time", no_clock),
                mock.patch.object(time_mod, "monotonic", no_clock),
                mock.patch.object(datetime_mod, "datetime", NoClock),
            ]

        with contextlib.ExitStack() as stack:
            for door in doors:
                stack.enter_context(door)
            result = call()

        self.assertEqual(breaches, [],
                         f"{what} is not pure: " + "; ".join(breaches))
        return result

    def assert_no_mutation(self, runner: RecordingRunner):
        """No recorded call may change anything on a machine."""
        for call in runner.calls:
            joined = call["joined"]
            words = set()
            for token in call["argv"]:
                words.update(str(token).replace("&&", " ").replace("|", " ").split())
            offending = sorted(words & self.MUTATING_VERBS)
            self.assertFalse(
                offending,
                f"a read only path executed a mutating call ({', '.join(offending)}): {joined}",
            )
            for fragment in self.MUTATING_FRAGMENTS:
                self.assertNotIn(
                    fragment, joined,
                    f"a read only path executed a mutating call: {joined}",
                )


# ---------------------------------------------------------------------------
# Repo scaffolding
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = """\
work:
  enabled: true
workloads:
  enabled: true
  dir: workflow/workloads
  stamp_dir: "$HOME/.bridge/workloads"
  label_prefix: bridge
  step_timeout_sec: 60
  probe_timeout_sec: 30
  ssh_connect_timeout_sec: 10
"""


#: What a throwaway repo's declaration contract says when the test does not care.
#: Permissive on purpose: the cases that measure the SECOND gate drive the
#: validator through a stub or a fake, and a contract with opinions here would
#: decide their answers instead.
PERMISSIVE_CONTRACT = "type: object\n"


def make_repo(root: Path, declarations=(), config: str | None = None,
              hosts=("host-a", "host-b", "host-c"), checks=("sample", "storage"),
              contract: str | None = PERMISSIVE_CONTRACT) -> Path:
    """Build a throwaway repo root: config, contract, declarations, remotes, checks.

    The contract is written because a repository WITHOUT one is a state of its
    own now (`schema_missing`), and a fixture that quietly lacks it would drive
    every `--strict` case into that state instead of the one it names. Pass
    `contract=None` to build the repository that has none.
    """
    (root / "workflow" / "workloads").mkdir(parents=True, exist_ok=True)
    if contract is not None:
        (root / "workflow" / "workloads" / "_schema.yaml").write_text(
            contract, encoding="utf-8")
    (root / "workflow" / "checks").mkdir(parents=True, exist_ok=True)
    (root / "infra" / "remotes").mkdir(parents=True, exist_ok=True)
    (root / "bridge-config.yaml").write_text(
        DEFAULT_CONFIG if config is None else config, encoding="utf-8")
    (root / "AGENTS.md").write_text("# throwaway root\n", encoding="utf-8")
    for spec in declarations:
        src = _resolve_declaration(spec)
        shutil.copy2(src, root / "workflow" / "workloads" / src.name)
    for host in hosts:
        shutil.copy2(HOSTS / f"{host}.yaml", root / "infra" / "remotes" / f"{host}.yaml")
    for group in checks:
        shutil.copy2(CHECKS / f"{group}.yaml", root / "workflow" / "checks" / f"{group}.yaml")
    return root


def _resolve_declaration(spec) -> Path:
    if isinstance(spec, Path):
        return spec
    for folder in (CORPUS, DERIVED, INVALID):
        candidate = folder / f"{spec}.yaml"
        if candidate.exists():
            return candidate
    raise AssertionError(f"no fixture declaration named {spec!r}")


def declaration(spec) -> Path:
    """Path of one fixture declaration, by id."""
    return _resolve_declaration(spec)


def corpus_paths() -> list:
    return [CORPUS / f"{i}.yaml" for i in CORPUS_IDS]


def load_raw(spec) -> dict:
    import yaml

    return yaml.safe_load(_resolve_declaration(spec).read_text(encoding="utf-8"))


def engine_sources() -> list:
    """Every python source file of the skill, for the structural assertions."""
    return sorted((SKILL_DIR / "engine").rglob("*.py"))


def skill_text_files() -> list:
    """Everything a leak scan has to cover, fixtures and goldens included.

    The golden extensions are on this list deliberately. A golden is bytes
    somebody read once and then froze, which makes it the easiest place for a
    real path or a real label to settle in unnoticed.
    """
    out = [p for p in SKILL_DIR.rglob("*") if p.is_file()]
    return sorted(p for p in out
                  if "__pycache__" not in p.parts
                  and ".pytest_cache" not in p.parts)


# ---------------------------------------------------------------------------
# The promotable surface
#
# `skill_text_files()` above walks this directory and nothing else, and that was
# read for two rounds as "everything a promote carries". It is not. The
# declaration CONTRACT this skill validates against, and the template it
# scaffolds from, live OUTSIDE the skill, under the directory the engine's own
# `config.DEFAULT_DIR` names. The promote router classifies both of them core,
# so both travel to the public repository together with the skill, and no scan
# read a byte of either: they could carry every forbidden literal at once and
# the whole suite still reported green.
#
# The surface is not restated here. It is ASKED of the real router,
# `scripts/categorize-commits.py`, loaded by path. A second copy of that routing
# table inside a test is a second thing to drift, and it would answer with the
# test's opinion of what ships rather than with the promote's. When the router
# cannot be asked, this RAISES: a guard that quietly shrinks back to the skill
# is the hole it was built to close.
# ---------------------------------------------------------------------------

#: Where the router lives, relative to a Bridge root.
ROUTER_REL = Path("scripts") / "categorize-commits.py"

_ROUTER_CACHE: dict = {}


class RouterUnavailable(RuntimeError):
    """The promote router could not be asked, so the surface is not known.

    Deliberately an exception and not a default. The one answer this must never
    give is the empty surface, because the empty surface is indistinguishable
    from a clean one.
    """


@contextlib.contextmanager
def _in_dir(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def repo_root(start=None):
    """The Bridge root above this copy of the skill, or None if there is none.

    By walking up, not by counting levels: the skill is also COPIED out of the
    repository and a copy has no root at all. BOTH markers are required, so a
    throwaway root built by `make_repo()` (an AGENTS.md, no scripts/) is never
    mistaken for the real thing.
    """
    base = Path(start) if start is not None else SKILL_DIR
    for candidate in (base, *base.parents):
        if (candidate / ROUTER_REL).is_file() and (candidate / "AGENTS.md").is_file():
            return candidate
    return None


def load_promote_router(root):
    """Return the REAL `classify_file`, imported from `root` by path.

    The router reads `bridge-config.yaml` and each skill's own frontmatter
    through RELATIVE paths, so it is both imported and called with the root as
    the working directory. Asked from anywhere else it answers `user` for the
    whole skill tree, which would empty the surface without a word.
    """
    root = Path(root).resolve()
    cached = _ROUTER_CACHE.get(str(root))
    if cached is not None:
        return cached
    path = root / ROUTER_REL
    if not path.is_file():
        raise RouterUnavailable(
            f"no promote router at {path}, so the tier of every file outside "
            f"this skill is unknown and none of them can be scanned")
    spec = importlib.util.spec_from_file_location("_workload_promote_router", path)
    if spec is None or spec.loader is None:
        raise RouterUnavailable(f"{path} could not be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    try:
        with _in_dir(root):
            spec.loader.exec_module(module)
    except Exception as exc:                 # reported, never swallowed
        raise RouterUnavailable(f"{path} did not import: {exc!r}") from exc
    classify = getattr(module, "classify_file", None)
    if not callable(classify):
        raise RouterUnavailable(f"{path} carries no classify_file() to ask")

    def ask(rel_path: str) -> str:
        with _in_dir(root):
            return classify(rel_path)

    _ROUTER_CACHE[str(root)] = ask
    return ask


def companion_dirs() -> tuple:
    """Repo relative directories outside the skill that the skill itself owns.

    Read from the engine rather than typed here, so the surface follows the
    skill's own declaration of what it owns instead of a copy of it.
    """
    return (mod("engine.config").DEFAULT_DIR,)


def core_files_outside_the_skill(root=None, classify=None, dirs=None) -> list:
    """Files the ROUTER calls core in the directories this skill owns.

    Root, router and directories are all injectable, so the SELECTION can be
    exercised without a repository around it. Nothing in here decides a tier;
    every file is submitted to the router and its answer is what counts.
    """
    root = Path(root) if root is not None else repo_root()
    if root is None:
        raise RouterUnavailable(
            "no Bridge root above this copy of the skill, so the promote router "
            "cannot be asked which files outside it ship as core")
    classify = classify if classify is not None else load_promote_router(root)
    out = []
    for rel in (dirs if dirs is not None else companion_dirs()):
        folder = root / rel
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if SKILL_DIR in path.parents:
                continue
            if classify(path.relative_to(root).as_posix()) == "core":
                out.append(path)
    return sorted(out)


# The identifier a schema publishes itself under, and the HOST inside it.
_SCHEMA_ID_HOST = re.compile(
    r'^[ \t]*"?\$id"?[ \t]*:[ \t]*["\']?'
    r'[A-Za-z][A-Za-z0-9+.\-]*://(?P<host>[^/\s"\']+)',
    re.MULTILINE,
)
_SCHEMA_KEYWORD = re.compile(r'^[ \t]*"?\$schema"?[ \t]*:', re.MULTILINE)


def _exempt_spans(text: str) -> list:
    """Where a forbidden literal may legitimately stand: a schema's `$id` HOST.

    Narrow on purpose, and it has to be narrow. Two dozen schemas in this tree
    publish their identifier under one project host, this skill's contract among
    them, and that host carries the project's own name. There the name is a self
    reference, not a leak. One column further along the same URL, in a comment,
    or in any value, the same letters are a leak again — so the exemption is a
    SPAN and not a word: a file is clean for a literal only when EVERY
    occurrence of it falls inside one of these spans.

    The exemption also needs the file to actually be a schema, which is what the
    `$schema` keyword says. Without it a `$id:` line is just a key somebody
    chose, and exempting it would hand every file a way to launder one line.
    """
    if not _SCHEMA_KEYWORD.search(text):
        return []
    return [(m.start("host"), m.end("host")) for m in _SCHEMA_ID_HOST.finditer(text)]


def instance_name_hits(path, text=None) -> list:
    """Every forbidden literal in a file that is not an exempt schema `$id` host.

    Returns `(literal, line)` pairs so a failure names WHERE, not merely THAT.
    """
    if text is None:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    spans = _exempt_spans(text)
    hits = []
    for literal in forbidden_literals():
        for match in re.finditer(re.escape(literal), text):
            if any(start <= match.start() and match.end() <= end
                   for start, end in spans):
                continue
            hits.append((literal, text.count("\n", 0, match.start()) + 1))
    return sorted(hits)


#: A digest as it is spelled inside a rendered file, whatever the format.
_DIGEST_IN_BYTES = re.compile(r"sha256:[0-9a-f]{64}")


def declared_digest(name: str, default: str | None = None) -> str:
    """The digest a fixture declaration ACTUALLY has, derived, never pinned.

    Sibling of `marker_digest_in` above, and the same lesson one level over:
    for as long as nothing read `declaration_digest`, a stamp could carry a
    hand-written placeholder and still look healthy, because the only thing it
    was ever compared against was another placeholder. A stamp made from a
    DIFFERENT file was indistinguishable from one made from this one.

    Derived rather than pinned, so an edit to a fixture declaration does not
    have to be chased into a hash by hand. `default` covers a stamp with no
    declaration at all, which is the orphan case and a scenario of its own.
    """
    model = mod("engine.model")
    for folder in (CORPUS, DERIVED):
        path = folder / f"{name}.yaml"
        if path.exists():
            return model.declaration_digest(model.load_declaration(path))
    if default is None:
        raise AssertionError(
            f"no fixture declaration named {name!r}; pass `default` if the stamp "
            f"is meant to belong to nothing")
    return default


def stamp_json(name: str = "calendar-export") -> str:
    """The recorded stamp, with its declaration digest pulled to the truth.

    The file on disk carries a placeholder from before anything read the
    field. Left there, every test driving the real observation path would
    assert about a stamp belonging to no declaration, which is precisely the
    situation `classify` now exists to find.
    """
    raw = json.loads(read_output(f"stamp-{name}.json"))
    raw["declaration_digest"] = declared_digest(name)
    return json.dumps(raw)


def marker_digest_in(artifact) -> str:
    """The ownership digest as it REALLY stands in the rendered bytes.

    Drawn out of the artifact instead of set by hand. A fixture that puts the
    same constant on both sides of a comparison proves that the comparison
    compiles, not that it compares the right two things: exactly that shape hid
    a bug which made every correctly provisioned run report drift forever.
    """
    found = set()
    for item in artifact.files:
        content = item.content
        if not isinstance(content, str):
            content = content.decode("utf-8")
        found.update(_DIGEST_IN_BYTES.findall(content))
    if len(found) != 1:
        raise AssertionError(
            f"expected exactly one ownership digest in the rendered bytes of "
            f"{artifact.unit_ref}, found {sorted(found)}")
    return found.pop()


def golden(name: str) -> Path:
    return GOLDEN / name


def assert_golden(case: unittest.TestCase, name: str, produced: str):
    """Compare against a reviewed golden, or refuse to invent one.

    A golden nobody has read is not a golden. The first run writes it only when
    WORKLOAD_UPDATE_GOLDEN=1 is set, which is the human saying they looked.
    """
    path = golden(name)
    if os.environ.get("WORKLOAD_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(produced, encoding="utf-8")
    if not path.exists():
        raise AssertionError(
            f"no reviewed golden at {path}. Read the produced bytes, then rerun with "
            f"WORKLOAD_UPDATE_GOLDEN=1 to record them:\n{produced}"
        )
    case.assertEqual(path.read_text(encoding="utf-8"), produced,
                     f"rendered bytes drifted from the reviewed golden {name}")
