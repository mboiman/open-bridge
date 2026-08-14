"""Contract + guards for the optional OTLP log export (``_runtime.otlp_logging``).

Logging fails without an error message: a missing anchor, a wrong endpoint or an
unauthorised key all look like "a quiet day" from the outside. These tests lock
the parts that a runtime probe cannot see, and the last two are *guards* in the
sense of skills/bks-logging: they check the SOURCE against the wiring, not
behaviour on one example.

The OpenTelemetry SDK is an optional dependency. Tests that need it skip cleanly
when it is absent, so the suite stays green on a minimal runtime install.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from _runtime.otlp_logging import (
    APPLICATION_LOGGER_ANCHORS,
    TRANSCRIPT_LOGGER_NAME,
    _channel,
    dataset_for,
    resolve_headers,
    resource_attributes,
    setup_otlp_logging,
    setup_transcript_logging,
    shutdown_otlp_logging,
)

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "_runtime"


def _channel_provider(name: str):
    """The LoggerProvider of one export channel, for asserting its resource."""
    return _channel(name)["provider"]


@pytest.fixture(autouse=True)
def _detach_after_each_test():
    """Leave no handler behind.

    A handler that survives the test would still be attached at interpreter
    shutdown, where the SDK tries to start a flush thread and Python refuses.
    That produced a traceback AFTER a green run, which is exactly the kind of
    noise that trains people to ignore output.
    """
    yield
    shutdown_otlp_logging()


# --------------------------------------------------------------------------
# configuration resolution
# --------------------------------------------------------------------------

def test_console_only_without_configuration(monkeypatch):
    """No endpoint means console only — and never an exception.

    A logging problem must not take the agent down; that is the whole point of
    returning a status instead of raising.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    status, enabled = setup_otlp_logging("alice")
    assert enabled is False
    assert status.startswith("CONSOLE_ONLY")


def test_headers_from_standard_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=ApiKey abc123,x-tenant=bks")
    assert resolve_headers() == {"Authorization": "ApiKey abc123", "x-tenant": "bks"}


def test_headers_from_keyvault_secret(monkeypatch):
    """The key is read at runtime via az — never from a file, never from the plist."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.setenv("OTEL_AUTH_SECRET_VAULT", "bks-lab-secrets")
    monkeypatch.setenv("OTEL_AUTH_SECRET_NAME", "BRIDGE-OTLP-INGEST-KEY")
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "S3CR3T\n"
        stderr = ""

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr("_runtime.otlp_logging.subprocess.run", fake_run)
    assert resolve_headers() == {"Authorization": "ApiKey S3CR3T"}
    assert "keyvault" in calls[0] and "BRIDGE-OTLP-INGEST-KEY" in calls[0]


def test_headers_empty_when_secret_unreadable(monkeypatch):
    """An unreadable vault degrades to console only, it does not crash the agent."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.setenv("OTEL_AUTH_SECRET_VAULT", "bks-lab-secrets")
    monkeypatch.setenv("OTEL_AUTH_SECRET_NAME", "BRIDGE-OTLP-INGEST-KEY")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "not found"

    monkeypatch.setattr("_runtime.otlp_logging.subprocess.run", lambda *a, **k: _Result())
    assert resolve_headers() == {}


def test_dataset_is_never_derived(monkeypatch):
    """Ein abgeleiteter Name ist plausibel und falsch, und das faellt nicht auf.

    Gemessen 2026-08-07 am zweiten Dienst: er startet als `--agent bks`, seine
    Registry deklariert `bridge_knowledge_agent`. Eine Ableitung aus dem
    Instanznamen haette beim ersten Dokument einen zweiten, undeklarierten Stream
    geoeffnet, der deklarierte waere fuer immer leer geblieben, und umbenennen geht
    danach nicht mehr. Beim ersten Dienst fiel es nur deshalb nicht auf, weil die
    Ableitung dort zufaellig den richtigen Namen ergab.
    """
    monkeypatch.delenv("OTEL_DATA_STREAM_DATASET", raising=False)
    assert dataset_for("alice") == ""
    assert dataset_for("bks") == ""


def test_setup_refuses_without_a_declared_dataset(monkeypatch):
    """Kein Ziel ist besser als ein falsches, und der Status sagt warum."""
    monkeypatch.delenv("OTEL_DATA_STREAM_DATASET", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    status, enabled = setup_otlp_logging("alice")
    assert enabled is False
    assert "OTEL_DATA_STREAM_DATASET" in status


def test_transcript_also_refuses_without_a_declared_dataset(monkeypatch):
    """Der Inhalts-Kanal muss GENAUSO verweigern, und das tat er zuerst nicht.

    Gemessen 2026-08-07 am zweiten Dienst, nachdem die Ableitung entfernt war:
    `setup_transcript_logging` baute sein Ziel als `f"{dataset_for(i)}_transcript"`.
    Bei leerer Basis ist das `"_transcript"`, also truthy, und die Verweigerung in
    `_setup_channel` griff nie. Ergebnis war die schlimmstmoegliche Richtung: der
    BETRIEBS-Kanal (harmlose Zeilen) verweigerte korrekt, waehrend der INHALTS-Kanal
    Besuchertext nach `logs-_transcript.otel-production` geschrieben haette, in einen
    Stream, den niemand deklariert hat und der sich nicht umbenennen laesst.

    Der Test der Betriebsseite allein konnte das nicht sehen, weil dort die Basis
    ungefiltert durchgereicht wird. Deshalb dieser eigene Test statt eines
    Parameters am bestehenden.
    """
    monkeypatch.delenv("OTEL_DATA_STREAM_DATASET", raising=False)
    monkeypatch.setenv("OTEL_TRANSCRIPT_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")

    status, enabled = setup_transcript_logging("alice")
    assert enabled is False
    assert "OTEL_DATA_STREAM_DATASET" in status
    assert _channel("transcript")["provider"] is None
    attached = {type(h).__name__ for h in logging.getLogger(TRANSCRIPT_LOGGER_NAME).handlers}
    assert "LoggingHandler" not in attached


def test_dataset_override_is_sanitised(monkeypatch):
    """Elastic allows only [a-z0-9_] in a dataset; a hyphen breaks the stream name."""
    monkeypatch.setenv("OTEL_DATA_STREAM_DATASET", "Bridge-Knowledge Agent")
    assert dataset_for("ignored") == "bridge_knowledge_agent"


def test_resource_attributes_carry_target_and_identity():
    attrs = resource_attributes(service_name="alice-agent", environment="PRODUCTION",
                                dataset="bridge_alice_agent")
    assert attrs["service.name"] == "alice-agent"
    assert attrs["deployment.environment"] == "PRODUCTION"
    assert attrs["data_stream.dataset"] == "bridge_alice_agent"
    # namespace is lower-cased: the data stream name is built from it verbatim.
    assert attrs["data_stream.namespace"] == "production"


def test_namespace_falls_back_when_environment_empty():
    attrs = resource_attributes(service_name="x", environment="", dataset="d")
    assert attrs["data_stream.namespace"] == "default"


# --------------------------------------------------------------------------
# handler wiring (needs the optional SDK)
# --------------------------------------------------------------------------

def _fake_exporter():
    pytest.importorskip("opentelemetry.sdk._logs")
    import opentelemetry.sdk._logs.export as otel_export

    # The SDK renamed LogExporter → LogRecordExporter while logs are unstable;
    # bind whichever this version ships instead of pinning a name that moves.
    base = getattr(otel_export, "LogRecordExporter", None) or otel_export.LogExporter

    class _Collecting(base):                                   # type: ignore[misc]
        def __init__(self):
            self.batches = []

        def export(self, batch):
            self.batches.append(batch)

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

        def shutdown(self):
            pass

    return _Collecting()


def test_handler_hangs_on_anchors_not_on_root(monkeypatch):
    """The root logger would drag in every third-party library.

    Half of what a root handler ships is somebody else's noise (HTTP clients whose
    URLs carry addresses, auth libraries, the web server). The price, taken
    knowingly: third-party ERRORs stay on the console.
    """
    exporter = _fake_exporter()
    monkeypatch.setenv("OTEL_DATA_STREAM_DATASET", "bridge_alice_agent")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    status, enabled = setup_otlp_logging("alice", exporter=exporter)
    assert enabled is True
    assert status == "OTLP_CONFIGURED"

    root_handlers = {type(h).__name__ for h in logging.getLogger().handlers}
    assert "LoggingHandler" not in root_handlers

    for anchor in APPLICATION_LOGGER_ANCHORS:
        names = {type(h).__name__ for h in logging.getLogger(anchor).handlers}
        assert "LoggingHandler" in names, f"anchor {anchor} carries no OTLP handler"


def test_shutdown_detaches_from_every_anchor(monkeypatch):
    monkeypatch.setenv("OTEL_DATA_STREAM_DATASET", "bridge_alice_agent")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    setup_otlp_logging("alice", exporter=_fake_exporter())
    shutdown_otlp_logging()
    for anchor in APPLICATION_LOGGER_ANCHORS:
        names = {type(h).__name__ for h in logging.getLogger(anchor).handlers}
        assert "LoggingHandler" not in names


def test_repeated_setup_does_not_stack_handlers(monkeypatch):
    """Two setups must replace, not append — otherwise every line is exported twice."""
    monkeypatch.setenv("OTEL_DATA_STREAM_DATASET", "bridge_alice_agent")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    setup_otlp_logging("alice", exporter=_fake_exporter())
    setup_otlp_logging("alice", exporter=_fake_exporter())
    anchor = logging.getLogger(APPLICATION_LOGGER_ANCHORS[0])
    otlp = [h for h in anchor.handlers if type(h).__name__ == "LoggingHandler"]
    assert len(otlp) == 1


# --------------------------------------------------------------------------
# static guards — source against wiring
# --------------------------------------------------------------------------

def _runtime_sources():
    return [p for p in RUNTIME_DIR.glob("*.py") if "__pycache__" not in p.parts]


def _logger_names(path: Path) -> list[str]:
    """Every ``logging.getLogger(...)`` argument in one file, as source text."""
    found = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "getLogger" and node.args:
            found.append(ast.unparse(node.args[0]))
        elif isinstance(fn, ast.Name) and fn.id == "getLogger" and node.args:
            found.append(ast.unparse(node.args[0]))
    return found


def test_the_source_tree_really_contains_loggers_to_check():
    """A guard that can never go red is not a guard.

    If the runtime ever switches back to a passed-around logger object, this
    fails first and says so, instead of the anchor test turning silently green.
    """
    total = sum(len(_logger_names(p)) for p in _runtime_sources())
    assert total >= 3, f"only {total} getLogger() call(s) found — is logging still module-level?"


def test_every_runtime_logger_hangs_below_an_anchor():
    """Anything not below an anchor never reaches the exporter — silently."""
    offenders = []
    for path in _runtime_sources():
        for name in _logger_names(path):
            if name == "__name__":
                continue                      # resolves to _runtime.* → anchored
            if not (name.startswith(("'", '"'))):
                # A computed name cannot be judged statically. Exactly one such
                # call is legitimate: the anchor resolver in otlp_logging itself,
                # which iterates APPLICATION_LOGGER_ANCHORS. Anywhere else it is a
                # hole in this guard and has to be named.
                if path.name != "otlp_logging.py":
                    offenders.append(f"{path.name}: getLogger({name}) — computed name")
                continue
            literal = name.strip("\"'")
            if not any(literal == a or literal.startswith(a + ".")
                       for a in APPLICATION_LOGGER_ANCHORS):
                offenders.append(f"{path.name}: getLogger({name})")
    assert offenders == [], (
        "logger outside the anchor list — its lines would never be exported:\n  "
        + "\n  ".join(offenders)
    )


def test_anchor_list_covers_both_module_identities():
    """``_runtime.x`` and ``agents._runtime.x`` are two separate logger trees.

    Both invocations are documented in agents/pyproject.toml. Anchoring only one
    of them exports half the lines and looks fine.
    """
    assert "_runtime" in APPLICATION_LOGGER_ANCHORS
    assert "agents._runtime" in APPLICATION_LOGGER_ANCHORS


def test_anchor_list_covers_the_entry_module():
    """Under ``python -m x.y`` the entry module's ``__name__`` IS ``__main__``.

    So ``getLogger(__name__)`` in server.py yields ``__main__`` at runtime, and
    without this anchor every startup line is dropped while /health cheerfully
    reports OTLP_CONFIGURED. Measured on 2026-08-06 against the live agent: the
    data stream was never even created.

    The static guard above cannot catch this — in the source it reads
    ``__name__``, which looks anchored. This assertion is the guard.
    """
    assert "__main__" in APPLICATION_LOGGER_ANCHORS


# --------------------------------------------------------------------------
# transcript channel — conversation content, separate stream, off by default
# --------------------------------------------------------------------------

def test_transcript_is_off_unless_explicitly_enabled(monkeypatch):
    """Recording conversations must never happen by accident.

    CORE ships this runtime to other people. A content channel that is on by
    default would record their visitors on their first deploy.
    """
    monkeypatch.delenv("OTEL_TRANSCRIPT_ENABLED", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    status, enabled = setup_transcript_logging("alice")
    assert enabled is False
    assert status.startswith("OFF")
    # pytest attaches its own capture handlers to every logger, so check for the
    # export handler specifically rather than for an empty list.
    attached = {type(h).__name__ for h in logging.getLogger(TRANSCRIPT_LOGGER_NAME).handlers}
    assert "LoggingHandler" not in attached


def test_transcript_writes_to_its_own_dataset(monkeypatch):
    """Own data stream, because a read key is scopable by stream and not by field."""
    monkeypatch.setenv("OTEL_TRANSCRIPT_ENABLED", "1")
    monkeypatch.setenv("OTEL_DATA_STREAM_DATASET", "bridge_alice_agent")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    status, enabled = setup_transcript_logging("alice", exporter=_fake_exporter())
    assert enabled is True
    assert status.startswith("RECORDING")

    provider = _channel_provider("transcript")
    attrs = provider.resource.attributes
    assert attrs["data_stream.dataset"] == "bridge_alice_agent_transcript"


def test_transcript_never_reaches_the_console(monkeypatch):
    """propagate=False, always.

    Otherwise the visitor's text lands in the service log on disk as well, and
    the whole point of one deliberate copy in one place is gone.
    """
    monkeypatch.setenv("OTEL_TRANSCRIPT_ENABLED", "1")
    monkeypatch.setenv("OTEL_DATA_STREAM_DATASET", "bridge_alice_agent")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    setup_transcript_logging("alice", exporter=_fake_exporter())
    assert logging.getLogger(TRANSCRIPT_LOGGER_NAME).propagate is False


def test_transcript_and_operational_channel_are_separate(monkeypatch):
    """The operational stream must not inherit the content stream's sensitivity."""
    monkeypatch.setenv("OTEL_TRANSCRIPT_ENABLED", "1")
    monkeypatch.setenv("OTEL_DATA_STREAM_DATASET", "bridge_alice_agent")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid")
    setup_otlp_logging("alice", exporter=_fake_exporter())
    setup_transcript_logging("alice", exporter=_fake_exporter())

    main_ds = _channel_provider("main").resource.attributes["data_stream.dataset"]
    tr_ds = _channel_provider("transcript").resource.attributes["data_stream.dataset"]
    assert main_ds != tr_ds

    # and the content logger is not one of the operational anchors
    assert TRANSCRIPT_LOGGER_NAME not in APPLICATION_LOGGER_ANCHORS
