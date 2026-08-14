"""Optional OTLP log export for a Bridge-Agent — vendor-neutral, opt-in.

Without configuration this module does nothing and the agent logs to the console
exactly as before. With ``OTEL_EXPORTER_OTLP_ENDPOINT`` set it attaches an OTLP
handler to the runtime's own logger trees and ships records to whatever OTLP
receiver is configured (a collector, a managed endpoint, a local sink).

CORE, therefore no vendor anywhere in the code: endpoint, auth header and target
naming all come from the environment. The OpenTelemetry SDK is an OPTIONAL
dependency (``uv sync --extra otlp``); when it is missing, the agent keeps
running and says so.

Derived from skills/bks-logging (Florian Hegenbarth), adapted from Azure
Functions to a long-running launchd/systemd process. Four constructions look
like overkill and are not:

* **Anchors, not the root logger.** A root handler ships every third-party line
  (HTTP clients whose URLs carry addresses, auth libraries, the web server). The
  price, taken knowingly: third-party ERRORs stay on the console, so a severity
  aggregation over the exported stream is not a health indicator.
* **State on ``logging.Logger.manager``, not module globals.** This runtime is
  importable as ``_runtime.x`` AND as ``agents._runtime.x`` (both invocations are
  documented in agents/pyproject.toml), which gives Python two module objects
  with separate globals. With module state, a second setup would stack handlers
  (every line twice) and ``flush()`` from the other copy would silently return
  False. ``logging.Logger.manager`` exists exactly once per process.
* **``atexit`` flush.** The exporter batches; without a flush the tail of the log
  is lost when the service is restarted.
* **No exception, ever.** A logging problem must not take the agent down. Every
  failure degrades to console and is reported through the status string, which
  belongs in ``/health`` — otherwise a deploy without logging config is
  indistinguishable from a healthy one.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

LOGS_PATH = "/v1/logs"

# The runtime's own logger trees. THREE identities, all of them real:
#
# * `_runtime.*`        — `python -m _runtime.server` (the launchd unit)
# * `agents._runtime.*` — `python -m agents._runtime.server` (from the repo root)
#   `logging` treats these as unrelated trees; anchoring one exports half the lines.
# * `__main__`          — the ENTRY module. Under `python -m x.y`, that module's
#   `__name__` is `__main__`, so `logging.getLogger(__name__)` in server.py yields
#   `__main__` at runtime, not `_runtime.server`. Measured on 2026-08-06: with only
#   the first two anchors, `/health` said OTLP_CONFIGURED and not a single startup
#   line arrived, because every line the entry module writes hangs under `__main__`.
#   A source-level guard cannot see this — in the source it reads `__name__`, which
#   looks anchored.
APPLICATION_LOGGER_ANCHORS = ("_runtime", "agents._runtime", "__main__")

# Instance tools live under agents/<instance>/tools and are invoked as separate
# processes, so they are not part of this tree by design.

# The transcript channel: question and answer verbatim, in a SEPARATE data
# stream, off unless `OTEL_TRANSCRIPT_ENABLED` is truthy.
#
# Separate rather than a field on the operational lines, because read access is
# scopable by STREAM and not by field — an API key can be limited to
# `logs-<dataset>.otel-*`, never to "everything except the message body". Mixing
# content into the operational stream would make the whole stream as sensitive as
# its most sensitive line, permanently.
#
# `propagate = False` on this logger, always: content must not reach the console
# handler and from there the service log. One copy, one place, on purpose.
TRANSCRIPT_LOGGER_NAME = "bridge.transcript"

_DATASET_ALLOWED = re.compile(r"[^a-z0-9_]+")
_STATE_ATTR = "_bridge_otlp_state"
_STATUS_UNSET = "CONSOLE_ONLY (logging export not initialised)"
_MAIN = "main"
_TRANSCRIPT = "transcript"


def _state() -> dict:
    state = getattr(logging.Logger.manager, _STATE_ATTR, None)
    if state is None:
        state = {"channels": {}}
        setattr(logging.Logger.manager, _STATE_ATTR, state)
    return state


def _channel(name: str) -> dict:
    return _state()["channels"].setdefault(
        name, {"provider": None, "handlers": [], "status": _STATUS_UNSET}
    )


def current_status() -> str:
    """The last setup result — belongs in the health endpoint."""
    return _channel(_MAIN)["status"]


def transcript_status() -> str:
    """Status of the content channel — also belongs in the health endpoint.

    Whether conversations are being recorded is exactly the kind of thing that
    must be visible from outside rather than inferred from a config file.
    """
    return _channel(_TRANSCRIPT)["status"]


def transcript_enabled() -> bool:
    return os.getenv("OTEL_TRANSCRIPT_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def transcript_logger() -> logging.Logger:
    """The logger that carries question and answer. Never propagates."""
    log = logging.getLogger(TRANSCRIPT_LOGGER_NAME)
    log.propagate = False
    return log


# ---------------------------------------------------------------------------
# configuration, all from the environment
# ---------------------------------------------------------------------------

def dataset_for(instance: str) -> str:
    """Target dataset name from the environment, sanitised. Never derived.

    Returns ``""`` when ``OTEL_DATA_STREAM_DATASET`` is unset, and the caller then
    refuses to export. That is deliberate and it replaces an earlier default of
    ``f"bridge_{instance}_agent"``.

    Why the default had to go, measured 2026-08-07 on the second service that
    used this module: it derives a name that is *plausible* and *wrong*. The first
    service was called ``alice`` and the derivation happened to produce exactly
    the declared name, so nothing showed. The second is started as ``--agent bks``
    while its registry declares ``bridge_knowledge_agent``; the derivation would
    have opened a second, undeclared stream on the first document, the declared
    one would have stayed empty forever, and a data stream cannot be renamed
    afterwards. Nothing would have reported it: the health endpoint looks the same
    either way.

    A wrong target is worse than no target. One line in the unit file is cheap;
    finding logs in a stream nobody declared is not.

    Only ``[a-z0-9_]`` survives sanitising, a hyphen would break the stream name.
    """
    raw = os.getenv("OTEL_DATA_STREAM_DATASET", "")
    return _DATASET_ALLOWED.sub("_", raw.strip().lower()).strip("_")


def _secret(vault: str, name: str) -> str:
    """Read a secret at runtime via the az CLI — never a file, never the plist.

    Same pattern the instance tools already use (AGENT_AZ_BIN + vault + name), so
    the launchd unit carries a NAME, not a value. Failures are logged by name
    only; the value never reaches a log line.
    """
    az = os.getenv("AGENT_AZ_BIN", "az")
    try:
        out = subprocess.run(
            [az, "keyvault", "secret", "show", "--vault-name", vault,
             "--name", name, "--query", "value", "-o", "tsv"],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("otlp: cannot run %s to read secret %s", az, name)
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        logger.warning("otlp: secret %s not readable from vault %s", name, vault)
        return ""
    return out.stdout.strip()


def resolve_headers() -> dict[str, str]:
    """Export headers, in order of precedence.

    1. ``OTEL_EXPORTER_OTLP_HEADERS`` (the OTel standard, ``k=v,k=v``)
    2. a secret resolved at runtime: ``OTEL_AUTH_SECRET_VAULT`` +
       ``OTEL_AUTH_SECRET_NAME``, rendered as
       ``{OTEL_AUTH_HEADER}: {OTEL_AUTH_SCHEME} {value}``

    Returns ``{}`` when nothing is configured or the secret cannot be read. An
    empty dict is legitimate (a local collector needs no auth) — the caller
    decides whether that is acceptable.
    """
    raw = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if raw:
        headers: dict[str, str] = {}
        for pair in raw.split(","):
            key, _, value = pair.partition("=")
            if key.strip():
                headers[key.strip()] = value.strip()
        return headers

    vault = os.getenv("OTEL_AUTH_SECRET_VAULT", "").strip()
    name = os.getenv("OTEL_AUTH_SECRET_NAME", "").strip()
    if not (vault and name):
        return {}
    value = _secret(vault, name)
    if not value:
        return {}
    header = os.getenv("OTEL_AUTH_HEADER", "Authorization")
    scheme = os.getenv("OTEL_AUTH_SCHEME", "ApiKey").strip()
    return {header: f"{scheme} {value}".strip()}


def resolve_endpoint() -> str:
    """Full logs endpoint. ``/v1/logs`` is appended when the base URL omits it."""
    base = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith(LOGS_PATH) else base + LOGS_PATH


def resource_attributes(*, service_name: str, environment: str, dataset: str) -> dict:
    """Resource attributes — they, not the URL, decide where records land.

    Set rather than inherited on purpose: a service that inherits its target
    writes into somebody else's stream, and the lines are then missing exactly
    where someone looks for them.
    """
    namespace = (environment or "").strip().lower() or "default"
    return {
        "service.name": service_name,
        "deployment.environment": environment,
        # Ignored by receivers that do not know them; required by those that do.
        "data_stream.dataset": dataset,
        "data_stream.namespace": namespace,
    }


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def application_loggers() -> list[logging.Logger]:
    return [logging.getLogger(name) for name in APPLICATION_LOGGER_ANCHORS]


def _install(target: logging.Logger, handler: logging.Handler, channel: str) -> None:
    target.addHandler(handler)
    _channel(channel)["handlers"].append((target, handler))


def _remove_previously_installed(channel: str) -> None:
    """A second setup must replace, not append — otherwise every line goes twice."""
    state = _channel(channel)
    for target, handler in state["handlers"]:
        if handler in target.handlers:
            target.removeHandler(handler)
    state["handlers"] = []
    if state["provider"] is not None:
        state["provider"].shutdown()
        state["provider"] = None


def flush(timeout_millis: int = 5000) -> bool:
    """Drain every batch buffer. Without it a restart drops the tail of the log."""
    flushed = False
    for state in _state()["channels"].values():
        provider = state["provider"]
        if provider is None:
            continue
        try:
            flushed = bool(provider.force_flush(timeout_millis)) or flushed
        except Exception:                                # noqa: BLE001 — never crash on shutdown
            pass
    return flushed


def shutdown_otlp_logging() -> None:
    """Flush, then detach every handler this module installed.

    The counterpart to the setup functions. Needed because a handler otherwise
    lives until interpreter shutdown, where the SDK tries to start a flush thread
    and Python refuses — a traceback after the real work is done.
    """
    flush()
    for name in list(_state()["channels"]):
        _remove_previously_installed(name)
        _channel(name)["status"] = _STATUS_UNSET


atexit.register(flush)


def _load_logging_handler():
    """The OTel handler class, preferring the maintained package.

    ``opentelemetry.sdk._logs.LoggingHandler`` still exists but the SDK marks it
    deprecated in favour of ``opentelemetry-instrumentation-logging``. Try the
    maintained one first and keep the SDK fallback, so a minimal install without
    the instrumentation package still exports.
    """
    try:
        from opentelemetry.instrumentation.logging.handler import LoggingHandler
        return LoggingHandler
    except ImportError:
        from opentelemetry.sdk._logs import LoggingHandler
        return LoggingHandler


def _setup_channel(
    channel: str,
    targets: list[logging.Logger],
    dataset: str,
    *,
    instance: str,
    environment: str | None,
    service_name: str | None,
    exporter,
    ok_status: str,
) -> tuple[str, bool]:
    """Build one export channel: provider, handler, attachment to ``targets``."""
    state = _channel(channel)
    _remove_previously_installed(channel)

    if not dataset:
        # Loud refusal instead of a plausible guess — see `dataset_for`.
        state["status"] = (
            "CONSOLE_ONLY (no OTEL_DATA_STREAM_DATASET set; refusing to guess a target)"
        )
        return state["status"], False

    endpoint = resolve_endpoint()
    if exporter is None and not endpoint:
        state["status"] = "CONSOLE_ONLY (no OTLP endpoint configured)"
        return state["status"], False

    if exporter is None:
        headers = resolve_headers()
        if not headers:
            logger.warning(
                "otlp: exporting without an auth header — fine for a local "
                "collector, a 401 at the receiver otherwise"
            )
        try:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        except ImportError:
            state["status"] = "CONSOLE_ONLY (opentelemetry exporter not installed)"
            return state["status"], False
        exporter = OTLPLogExporter(endpoint=endpoint, headers=headers)

    try:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        logging_handler_cls = _load_logging_handler()
    except ImportError:
        state["status"] = "CONSOLE_ONLY (opentelemetry sdk not installed)"
        return state["status"], False

    attributes = resource_attributes(
        service_name=service_name or os.getenv("OTEL_SERVICE_NAME") or f"{instance}-agent",
        environment=environment if environment is not None else os.getenv("ENVIRONMENT", ""),
        dataset=dataset,
    )
    provider = LoggerProvider(resource=Resource.create(attributes))
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    state["provider"] = provider

    handler = logging_handler_cls(level=logging.NOTSET, logger_provider=provider)
    for target in targets:
        _install(target, handler, channel)

    state["status"] = ok_status
    return state["status"], True


def setup_otlp_logging(
    instance: str,
    *,
    environment: str | None = None,
    service_name: str | None = None,
    exporter=None,
) -> tuple[str, bool]:
    """Attach the OTLP handler to the runtime's own loggers when configured.

    Returns ``(status, enabled)``. ``status`` is meant for the health endpoint:
    without it, a deploy that forgot the logging configuration looks exactly like
    a healthy one from the outside.
    """
    return _setup_channel(
        _MAIN,
        application_loggers(),
        dataset_for(instance),
        instance=instance,
        environment=environment,
        service_name=service_name,
        exporter=exporter,
        ok_status="OTLP_CONFIGURED",
    )


def setup_transcript_logging(
    instance: str,
    *,
    environment: str | None = None,
    service_name: str | None = None,
    exporter=None,
) -> tuple[str, bool]:
    """Attach a SECOND channel carrying question and answer verbatim.

    Off unless ``OTEL_TRANSCRIPT_ENABLED`` is truthy, and it says so in the
    status either way — whether conversations are recorded must be readable from
    outside, not inferred from a config file.

    Target dataset is ``<dataset>_transcript``, i.e. its own data stream, so a
    read key can be scoped to the operational lines alone. Content and operations
    in one stream would make the whole stream as sensitive as its worst line.
    """
    if not transcript_enabled():
        _channel(_TRANSCRIPT)["status"] = "OFF (no conversation content recorded)"
        return _channel(_TRANSCRIPT)["status"], False

    # Guard on the BASE name, before the suffix is appended. Composing first would
    # defeat the refusal in `_setup_channel`: with an undeclared base the string is
    # "_transcript", which is truthy, so the check there never fires and the channel
    # exports to `logs-_transcript.otel-<env>`. Measured 2026-08-07, and it fails in
    # the worst possible direction: the operational channel (harmless) refuses while
    # the content channel (visitor text) ships into an undeclared stream.
    base = dataset_for(instance)
    if not base:
        _channel(_TRANSCRIPT)["status"] = (
            "CONSOLE_ONLY (no OTEL_DATA_STREAM_DATASET set; refusing to guess a target)"
        )
        return _channel(_TRANSCRIPT)["status"], False

    return _setup_channel(
        _TRANSCRIPT,
        [transcript_logger()],
        f"{base}_transcript",
        instance=instance,
        environment=environment,
        service_name=service_name,
        exporter=exporter,
        ok_status="RECORDING (question and answer)",
    )
