"""The registry: the ONE place a runtime name is looked up.

Explicit imports, one dict, one accessor. That is the whole dispatch mechanism,
and it is why `render.py` contains no comparison against a runtime or a
platform name. Adding a backend is one file plus one line in `BACKENDS`.
"""

from __future__ import annotations

from engine import errors

# base first: the other modules reach for it while this package is still being
# imported, and the submodule attribute has to exist by then.
from . import base
from . import wrapper
from . import cron, dispatcher, inert, launchd, systemd


#: The default registry: the module-level instance of each backend, keyed by
#: the name a declaration writes in `placement.runtime`. Mutable on purpose,
#: because `configure` rebuilds it in place and every module that already holds
#: a reference then sees the configured instances.
BACKENDS = {
    launchd.LAUNCHD_USER.name: launchd.LAUNCHD_USER,
    launchd.LAUNCHD_SYSTEM.name: launchd.LAUNCHD_SYSTEM,
    systemd.SYSTEMD.name: systemd.SYSTEMD,
    cron.CRON.name: cron.CRON,
    dispatcher.DISPATCHER.name: dispatcher.DISPATCHER,
    inert.MANUAL.name: inert.MANUAL,
    inert.EXTERNAL.name: inert.EXTERNAL,
}


def get_backend(name: str):
    """The only lookup. An unknown runtime is refused by name."""
    try:
        return BACKENDS[name]
    except KeyError:
        raise errors.UnknownBackend(
            f"no backend named {name!r}; this Bridge carries: "
            + ", ".join(sorted(BACKENDS))
        ) from None


def configure(cfg) -> dict:
    """Rebuild every backend from the live configuration.

    The label prefix, the dispatcher's registry and the guarantees it claims
    all come from there, so nothing instance-specific can settle into a source
    file. Callers that hold `BACKENDS` keep working: it is rebuilt in place.
    """
    user, system = launchd.build(cfg)
    manual, external = inert.build(cfg)
    rebuilt = [user, system, systemd.build(cfg), cron.build(cfg),
               dispatcher.build(cfg), manual, external]
    BACKENDS.clear()
    BACKENDS.update({backend.name: backend for backend in rebuilt})
    return BACKENDS
