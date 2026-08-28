"""Where a workload runs: the remote inventory, and what each platform carries.

Reads `infra/remotes/<slug>.yaml` for the platform, the ssh target and the
declared service list. It reads nothing else out of that file, and it never
writes one: this skill owns the workload declarations, not the inventory.
"""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import HostUnknown

REMOTES_DIR = "infra/remotes"
LOCAL = "local"

#: Which runtime each platform can actually carry. A False here becomes an
#: UnsupportedRuntime at render time, naming both the platform and the runtime.
SUPPORT_MATRIX = {
    "macos": ("launchd", "launchd-system", "cron", "dispatcher"),
    "linux": ("systemd", "cron", "dispatcher"),
}

_UNAME_TO_PLATFORM = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}


@dataclass
class Host:
    """One machine. The field names are shared with the test fakes."""

    slug: str
    platform: str = ""
    is_local: bool = False
    #: Why this named host is being read locally, empty when it is not. It
    #: exists to be PRINTED: a local read is the one decision here that could
    #: quietly answer about the wrong machine, and a reader noticing is the
    #: cheapest guard there is.
    local_reason: str = ""
    ssh_user: str = ""
    ssh_host: str = ""
    ssh_port: int = 22
    timezone: str | None = None
    reachable: bool = True
    services: list = field(default_factory=list)
    source: str = ""

    @property
    def ssh_target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}" if self.ssh_user else self.ssh_host


def local_platform() -> str:
    """Detected, never assumed."""
    return _UNAME_TO_PLATFORM.get(_platform.system(), _platform.system().lower())


#: Where a machine writes down which register entry it is. A MARKER, never a
#: name: `hostname` on one of these machines returns whatever the router hands
#: out, and the same rule already governs how a served directory is proved to
#: be ours.
IDENTITY_FILE = ".bridge/host-identity"


def local_identity(home=None) -> str:
    """Which register entry this machine says it is. Empty when it says nothing."""
    import os
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    try:
        said = (base / IDENTITY_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    return said.strip().splitlines()[0].strip() if said.strip() else ""


def resolve_host(slug: str, root: Path, *, home=None) -> Host:
    """Resolve a placement host against the remote inventory.

    A named host may turn out to BE the machine running this, in which case it
    is read locally rather than over ssh. Not a convenience: the machine this
    skill exists to watch cannot reach itself over ssh at all (measured twice,
    `Permission denied`), and a key for it would be a new credential for a
    problem that needs none. It would also be the wrong measurement, because an
    ssh session carries its own identity and its own grants, while the honest
    probe runs where the service manager runs.
    """
    if slug == LOCAL:
        return Host(slug=LOCAL, platform=local_platform(), is_local=True,
                    ssh_host=LOCAL, source=LOCAL)
    path = Path(root) / REMOTES_DIR / f"{slug}.yaml"
    if not path.exists():
        raise HostUnknown(
            f"no machine named {slug!r} under {REMOTES_DIR}/", host=slug, path=str(path))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ssh = raw.get("ssh") or {}
    # The marker decides only the WAY there. Everything else still comes from
    # the register entry, so a local read costs no inventory.
    mine = local_identity(home)
    here = bool(mine) and mine == slug
    return Host(
        slug=str(raw.get("name", slug)),
        platform=str(raw.get("type", "")),
        is_local=here,
        local_reason=(f"this machine names itself {mine!r} in ~/{IDENTITY_FILE}, "
                      f"so it was read locally instead of over ssh") if here else "",
        ssh_user=str(ssh.get("user", "")),
        ssh_host=str(ssh.get("host", slug)),
        ssh_port=int(ssh.get("port", 22)),
        timezone=raw.get("timezone"),
        services=list(raw.get("services") or []),
        source=str(path),
    )


def supports(host: Host, runtime: str) -> bool:
    """The platform matrix. An unknown platform carries nothing at all."""
    return runtime in SUPPORT_MATRIX.get(host.platform, ())
