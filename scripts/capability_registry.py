#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Standalone reader/writer of the machine-global capability registry.

`~/.bridge-capabilities/<capability-type>.yaml` — one file PER CAPABILITY TYPE
(e.g. `transcription.yaml`), each holding a LIST of entries — declares that a
capability exists on this machine and how to reach it, so a sibling Bridge
instance can find shared infrastructure (a transcription worker, a backup
pipeline, a remote fleet) WITHOUT reading another instance's files. See
`docs/capability-registry.md` for the design and `docs/multi-instance.md` for
the data-isolation rule this deliberately does NOT weaken.

This is a SEPARATE namespace from `~/.workspaces/` (`scripts/workspace_registry.py`)
— capabilities are a different concept from project identity (see the module
docstring there vs. `docs/capability-registry.md` § Why not workspaces). It
reuses the SAME protocol SHAPE (advisory lock → whole-file read → modify in
memory → atomic replace → unlock — see `skills/workspace/references/model.md`
§ "The multi-writer protocol"), via the shared, registry-agnostic primitives in
`lib/registry_io.py` — NOT the workspace registry's own file or schema.

Unlike the workspace registry, THIS registry is single-owner: only Bridge
instances write it (no external tool conforms to it), so there is no foreign-
extension-slice preservation, no adopt/merge logic, no cross-tool identity
matching. What IS load-bearing, and mechanically enforced here rather than
left to a human to remember, is the CLOSED entry schema (`ALLOWED_ENTRY_FIELDS`
below): an entry can carry "a capability exists and how to reach it" — never
context names, speakers, customer data, or a path into another instance.

Read path is FAIL-OPEN: a missing type file, or a missing directory, just
means "no capability registered" — not an error. Reading a declaration a
sibling instance chose to publish is not a scan (see `docs/capability-
registry.md` § Read path is fail-open); no permission gate belongs here.
Write path is FAIL-CLOSED on anomalies, same discipline as the workspace
registry: an unparseable file or an unsupported version refuses the write
rather than guessing or clobbering.

CLI:
    capability_registry.py [--registry-dir D] path
    capability_registry.py [--registry-dir D] list-types
    capability_registry.py [--registry-dir D] read <type>
    capability_registry.py [--registry-dir D] list <type>
    capability_registry.py [--registry-dir D] publish <type> --provider P --registered-by NAME
        [--host H] [--launchd-label L] [--contexts-dir D]
    capability_registry.py [--registry-dir D] remove <type> --registered-by NAME [--provider P]
"""

import argparse
import glob
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.registry_io import (  # noqa: E402  (path setup must precede this import)
    AdvisoryLock,
    atomic_write_bytes,
    coerce_version,
    now_iso,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_DIR = "~/.bridge-capabilities"
LOCK_SUFFIX = ".lock"
TMP_SUFFIX = ".tmp"

#: Highest on-disk `version` this writer understands. Writes emit exactly this;
#: a file whose version exceeds it is read-only (writing is refused) — same
#: fail-closed discipline as `workspace_registry.MAX_SUPPORTED_VERSION`.
MAX_SUPPORTED_VERSION = 1

#: The CLOSED set of fields an entry may carry. This is the mechanical half of
#: the issue's own hard requirement ("what must never enter it: context names,
#: speakers, customer data, paths into other instances") — a field not in this
#: set is refused at write time, not merely discouraged in a comment. Extending
#: this set for a new capability type (backups, remote fleet, ...) is a
#: deliberate code change here, not a runtime-open door.
ALLOWED_ENTRY_FIELDS = frozenset({
    "provider",        # which skill/tool serves this capability (e.g. "meeting-transcription")
    "host",             # SSH/Tailscale alias of the machine serving it (optional — "local" needs none)
    "launchd_label",    # the service's launchd/systemd unit name (optional)
    "contexts_dir",     # where the provider's runtime config lives (optional, a PATH ON THE HOST — never a bridge-repo path)
    "registered_by",    # the NAME of the instance that published this entry — never a path into it
    "registered_at",    # ISO-8601 UTC timestamp of the last publish (staleness signal, not a liveness guarantee)
})

#: Fields a caller must supply for a valid entry — everything else is optional.
REQUIRED_ENTRY_FIELDS = frozenset({"provider", "registered_by", "registered_at"})


class RegistryError(Exception):
    """User-facing, fail-closed error — printed without a traceback (exit 1)."""


class RegistryVersionError(RegistryError):
    """The on-disk file is newer than we understand — refuse to WRITE it (exit 4)."""


class RegistrySchemaError(RegistryError):
    """An entry field is outside the closed allowlist — refuse to WRITE it."""


def _type_slug(capability_type: str) -> str:
    if not isinstance(capability_type, str) or not capability_type:
        raise RegistryError("a capability type is required (e.g. 'transcription')")
    import re
    if not re.match(r"^[a-z][a-z0-9_-]*$", capability_type):
        raise RegistryError(
            f"invalid capability type {capability_type!r} — expected "
            f"[a-z][a-z0-9_-]*, e.g. 'transcription'")
    return capability_type


def _validate_entry(entry: dict) -> None:
    unknown = set(entry) - ALLOWED_ENTRY_FIELDS
    if unknown:
        raise RegistrySchemaError(
            f"entry carries field(s) outside the closed allowlist: "
            f"{sorted(unknown)} — allowed: {sorted(ALLOWED_ENTRY_FIELDS)}. "
            f"The registry may only say a capability exists and how to reach "
            f"it — never context/customer/speaker data.")
    missing = REQUIRED_ENTRY_FIELDS - set(entry)
    if missing:
        raise RegistryError(f"entry is missing required field(s): {sorted(missing)}")


class Registry:
    """A conformant reader/writer of one `<capability-type>.yaml` file."""

    def __init__(self, capability_type: str, directory: str | None = None):
        self.capability_type = _type_slug(capability_type)
        self.dir = os.path.abspath(
            os.path.expanduser(
                directory or os.environ.get("BRIDGE_CAPABILITIES_DIR") or DEFAULT_DIR
            )
        )
        self.path = os.path.join(self.dir, f"{self.capability_type}.yaml")
        self.tmp_path = self.path + TMP_SUFFIX
        self.lock_path = self.path + LOCK_SUFFIX

    @staticmethod
    def list_types(directory: str | None = None) -> list[str]:
        """Every capability type with a registry file present, alphabetically."""
        d = os.path.abspath(
            os.path.expanduser(directory or os.environ.get("BRIDGE_CAPABILITIES_DIR") or DEFAULT_DIR)
        )
        if not os.path.isdir(d):
            return []
        out = []
        for f in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            base = os.path.basename(f)
            if base.startswith("."):
                continue
            out.append(base[: -len(".yaml")])
        return out

    # --- read side (lockless; atomic-replace guarantees a whole-file read) ----

    @staticmethod
    def _empty() -> dict:
        return {"version": MAX_SUPPORTED_VERSION, "entries": []}

    def _parse(self, raw: bytes) -> dict:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("registry root is not a mapping")
        entries = data.get("entries")
        if entries is None:
            data["entries"] = []
        elif not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError("registry 'entries' is not a list of mappings")
        return data

    def read_registry(self) -> dict:
        """Return the whole type-file (a copy). Missing file → empty — FAIL-OPEN.

        A missing capability registry means "nothing declared", not an error —
        reading a declaration is not a scan (see the module docstring). A
        present-but-corrupt file still raises (a write would refuse it too).
        """
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "rb") as fh:
                return self._parse(fh.read())
        except (ValueError, yaml.YAMLError, UnicodeDecodeError) as exc:
            raise RegistryError(
                f"registry {self.path} is unreadable ({exc}) — inspect or "
                f"remove it; refusing to guess.")

    def list_entries(self) -> list[dict]:
        return list(self.read_registry().get("entries") or [])

    # --- write side (ONLY inside the lock) ------------------------------------

    def _read_for_write(self) -> dict:
        """Read the base to modify while holding the lock — FAIL-CLOSED.

        Mirrors `workspace_registry.Registry._read_for_write`: an unparseable
        file or a missing/non-numeric `version` refuses the write untouched; a
        version newer than we understand refuses the write (reading stays
        allowed). There is no legacy version to rotate for this registry (it
        starts at v1), so unlike the workspace registry there is no `.bak` path.
        """
        if not os.path.exists(self.path):
            return self._empty()
        with open(self.path, "rb") as fh:
            raw = fh.read()
        try:
            data = self._parse(raw)
        except (ValueError, yaml.YAMLError, UnicodeDecodeError):
            raise RegistryError(
                f"registry {self.path} is unreadable — inspect or remove it; "
                f"refusing to guess (a write must not overwrite an unparseable "
                f"file).")
        version = coerce_version(data.get("version"))
        if version is None:
            raise RegistryError(
                f"registry {self.path} has a missing or non-numeric 'version' "
                f"— inspect or remove it; refusing to guess.")
        if version > MAX_SUPPORTED_VERSION:
            raise RegistryVersionError(
                f"registry {self.path} is version {version}; this writer "
                f"understands at most {MAX_SUPPORTED_VERSION}. Refusing to "
                f"write (a newer writer's semantics must not be clobbered). "
                f"Reading is still allowed.")
        return data

    def _write(self, data: dict) -> None:
        data["version"] = MAX_SUPPORTED_VERSION
        body = yaml.safe_dump(data, default_flow_style=False, sort_keys=False,
                               allow_unicode=True)
        atomic_write_bytes(self.path, body.encode("utf-8"), tmp_path=self.tmp_path)

    # --- public writer API ----------------------------------------------------

    def publish(self, provider: str, registered_by: str, host: str | None = None,
                launchd_label: str | None = None, contexts_dir: str | None = None) -> dict:
        """Create-or-update THIS instance's entry for `provider` under this type.

        Keyed by `(provider, registered_by)` — an instance re-publishing the
        same provider updates its own row in place (fresh `registered_at`,
        never a duplicate); a DIFFERENT instance's entry for the same provider
        is left untouched (each instance owns only its own rows, matched by
        `registered_by`, never by content).
        """
        if not isinstance(provider, str) or not provider:
            raise RegistryError("publish needs a non-empty provider")
        if not isinstance(registered_by, str) or not registered_by:
            raise RegistryError("publish needs a non-empty registered_by (instance name)")

        entry = {"provider": provider, "registered_by": registered_by,
                  "registered_at": now_iso()}
        if host:
            entry["host"] = host
        if launchd_label:
            entry["launchd_label"] = launchd_label
        if contexts_dir:
            entry["contexts_dir"] = contexts_dir
        _validate_entry(entry)

        with AdvisoryLock(self.lock_path):
            data = self._read_for_write()
            entries: list[dict] = data.setdefault("entries", [])
            for i, e in enumerate(entries):
                if e.get("provider") == provider and e.get("registered_by") == registered_by:
                    entries[i] = entry
                    break
            else:
                entries.append(entry)
            self._write(data)
            return dict(entry)

    def remove(self, registered_by: str, provider: str | None = None) -> int:
        """Remove THIS instance's own entries — NEVER another instance's.

        Matches strictly on `registered_by`; `provider` further narrows to one
        provider's entry when given. Returns the number of entries removed
        (0 is a normal, expected result — a safe no-op when nothing was ever
        published, e.g. `share_capability` was flipped off without ever having
        been on).
        """
        if not isinstance(registered_by, str) or not registered_by:
            raise RegistryError("remove needs a non-empty registered_by (instance name)")

        with AdvisoryLock(self.lock_path):
            data = self._read_for_write()
            entries: list[dict] = data.setdefault("entries", [])
            keep, dropped = [], 0
            for e in entries:
                mine = e.get("registered_by") == registered_by
                match = mine and (provider is None or e.get("provider") == provider)
                if match:
                    dropped += 1
                else:
                    keep.append(e)
            if dropped:
                data["entries"] = keep
                self._write(data)
            return dropped


# ---------------------------------------------------------------------------
# CLI (thin — the library API above is the real surface)
# ---------------------------------------------------------------------------

def _print_entries(entries: list[dict]) -> None:
    if not entries:
        print("No entries registered.")
        return
    for e in entries:
        extra = " ".join(f"{k}={v}" for k, v in e.items()
                          if k not in ("provider", "registered_by", "registered_at"))
        print(f"{e.get('provider', '?'):<24} by={e.get('registered_by', '?'):<16} "
              f"at={e.get('registered_at', '?')}  {extra}")


def cmd_path(_reg, args) -> int:
    d = os.path.abspath(os.path.expanduser(
        args.registry_dir or os.environ.get("BRIDGE_CAPABILITIES_DIR") or DEFAULT_DIR))
    print(d)
    return 0


def cmd_list_types(_reg, args) -> int:
    types = Registry.list_types(args.registry_dir)
    if not types:
        print("No capability types registered.")
        return 0
    for t in types:
        print(t)
    return 0


def cmd_read(reg: Registry, _args) -> int:
    print(yaml.safe_dump(reg.read_registry(), default_flow_style=False, sort_keys=False,
                          allow_unicode=True), end="")
    return 0


def cmd_list(reg: Registry, _args) -> int:
    _print_entries(reg.list_entries())
    return 0


def cmd_publish(reg: Registry, args) -> int:
    entry = reg.publish(args.provider, args.registered_by, host=args.host,
                         launchd_label=args.launchd_label, contexts_dir=args.contexts_dir)
    print(f"published {entry['provider']} (by {entry['registered_by']})")
    return 0


def cmd_remove(reg: Registry, args) -> int:
    n = reg.remove(args.registered_by, provider=args.provider)
    print(f"removed {n} entr{'y' if n == 1 else 'ies'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capability-registry",
        description="Standalone reader/writer of the machine-global capability "
                    "registry (~/.bridge-capabilities/<type>.yaml).")
    p.add_argument("--registry-dir", dest="registry_dir",
                   help="registry dir (default: $BRIDGE_CAPABILITIES_DIR or ~/.bridge-capabilities)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("path", help="print the resolved registry directory").set_defaults(func=cmd_path, needs_type=False)
    sub.add_parser("list-types", help="list capability types with a registry file present").set_defaults(func=cmd_list_types, needs_type=False)

    rd = sub.add_parser("read", help="print one capability type's whole file as YAML")
    rd.add_argument("type")
    rd.set_defaults(func=cmd_read, needs_type=True)

    li = sub.add_parser("list", help="list entries for one capability type")
    li.add_argument("type")
    li.set_defaults(func=cmd_list, needs_type=True)

    pu = sub.add_parser("publish", help="create-or-update this instance's entry")
    pu.add_argument("type")
    pu.add_argument("--provider", required=True)
    pu.add_argument("--registered-by", required=True, dest="registered_by")
    pu.add_argument("--host")
    pu.add_argument("--launchd-label", dest="launchd_label")
    pu.add_argument("--contexts-dir", dest="contexts_dir")
    pu.set_defaults(func=cmd_publish, needs_type=True)

    rm = sub.add_parser("remove", help="remove this instance's own entries")
    rm.add_argument("type")
    rm.add_argument("--registered-by", required=True, dest="registered_by")
    rm.add_argument("--provider")
    rm.set_defaults(func=cmd_remove, needs_type=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    reg = Registry(args.type, args.registry_dir) if args.needs_type else None
    try:
        return args.func(reg, args)
    except RegistryVersionError as exc:
        sys.stderr.write(f"capability-registry: {exc}\n")
        return 4
    except RegistryError as exc:
        sys.stderr.write(f"capability-registry: {exc}\n")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        sys.stderr.write("\ncapability-registry: interrupted\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
