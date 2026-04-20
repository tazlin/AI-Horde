# SPDX-FileCopyrightText: 2024 Haidra Developers
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os

import logfire

from horde.logger import logger

_initialized = False


def init_telemetry(app):
    """Initialize Logfire (OTel) instrumentation and optional Pyroscope profiling.

    Requires OTEL_EXPORTER_OTLP_ENDPOINT env var (e.g. http://127.0.0.1:4318)
    to route telemetry to the Grafana stack via Alloy.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        logger.init_warn("Telemetry", status="Disabled")
        return

    # Start Pyroscope profiler first, then wire span processor into Logfire
    span_processors = _init_pyroscope()

    logfire.configure(
        send_to_logfire=False,
        console=False,
        service_name=os.environ.get("OTEL_SERVICE_NAME", "ai-horde"),
        additional_span_processors=span_processors or None,
    )

    logfire.instrument_flask(app)
    logger.init_ok("Telemetry", status="Flask")

    # Rebind metric instruments against the post-configure meter so they
    # actually export. See the comment near `_meter` below.
    _bind_instruments()
    logger.init_ok("Telemetry", status="Metrics")

    # SQLAlchemy — must be called after db.engine exists (i.e. within app context)
    from horde.flask import db

    with app.app_context():
        logfire.instrument_sqlalchemy(engine=db.engine)
    logger.init_ok("Telemetry", status="SQLAlchemy")

    # Redis instrumentation — disabled by default because each horde_r_* wrapper
    # issues several commands, which can dominate trace volume. Enable with
    # OTEL_INSTRUMENT_REDIS=true; Alloy-side span filtering is expected to drop
    # fast (<2ms) Redis spans while retaining slow cache anomalies.
    if os.environ.get("OTEL_INSTRUMENT_REDIS", "").lower() == "true":
        try:
            logfire.instrument_redis()
            logger.init_ok("Telemetry", status="Redis")
        except Exception as err:
            logger.init_warn("Telemetry", status=f"Redis: {err}")

    # Outbound HTTP (webhooks etc.) — inject W3C traceparent so downstream
    # services can correlate with Horde traces and emit child spans for each
    # request, including retries.
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
        logger.init_ok("Telemetry", status="Requests")
    except ImportError:
        logger.init_warn(
            "Telemetry",
            status="Requests N/A (pip install opentelemetry-instrumentation-requests)",
        )
    except Exception as err:
        logger.init_warn("Telemetry", status=f"Requests: {err}")

    # Bridge loguru → OTel logs so every log record carries trace_id/span_id
    loguru_handler = logfire.loguru_handler()
    if isinstance(loguru_handler, dict):
        logger.add(**loguru_handler)
    else:
        logger.add(loguru_handler)
    logger.init_ok("Telemetry", status="Loguru")

    logger.init_ok("Telemetry", status="Ready")


def _init_pyroscope():
    """Start Pyroscope continuous profiling and return span processors for trace-profile linking."""
    if os.environ.get("PYROSCOPE_ENABLED", "").lower() != "true":
        return []

    try:
        import pyroscope  # noqa: F811

        pyroscope.configure(
            application_name=os.environ.get("OTEL_SERVICE_NAME", "ai-horde"),
            server_address=os.environ.get("PYROSCOPE_SERVER_ADDRESS", "http://localhost:4040"),
            tags={
                "environment": os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
            },
            tenant_id=os.environ.get("PYROSCOPE_TENANT_ID"),
        )
        logger.init_ok("Telemetry", status="Pyroscope")
    except ImportError:
        logger.init_warn("Telemetry", status="Pyroscope N/A")
        return []
    except Exception as err:
        logger.init_err("Telemetry", status=f"Pyroscope: {err}")
        return []

    # Link OTel spans → Pyroscope profiles via pyroscope.profile.id attribute
    try:
        from pyroscope.otel import PyroscopeSpanProcessor

        logger.init_ok("Telemetry", status="Pyroscope span profiles")
        return [PyroscopeSpanProcessor()]
    except ImportError:
        logger.init_warn("Telemetry", status="pyroscope-otel N/A (pip install pyroscope-otel)")
        return []


def get_traceparent():
    """Capture the current W3C traceparent string from the active span context."""
    from opentelemetry import trace
    from opentelemetry.trace import format_span_id, format_trace_id

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return f"00-{format_trace_id(ctx.trace_id)}-{format_span_id(ctx.span_id)}-{ctx.trace_flags:02x}"
    return None


# ---------------------------------------------------------------------------
# OTel Metrics — recorded inside active spans so exemplars (trace→metric
# links) are automatically attached by the SDK when using histogram views.
#
# Instruments must be acquired AFTER `logfire.configure()` has installed the
# real MeterProvider, otherwise they bind to logfire's pre-configure proxy
# state and never export (observed: `logfire.metric_histogram(...)` or even
# `opentelemetry.metrics.get_meter(...).create_histogram(...)` called at
# module import time silently no-op because logfire's ProxyMeterProvider
# does not retroactively wire early instruments into its reader pipeline
# the same way it wires instruments created post-configure).
#
# We keep module-level names so existing call sites (`_generate_duration
# .record(...)`, etc.) keep working — they start as OTel NoOp instruments
# and are swapped to real ones by `_bind_instruments()` from init_telemetry.
# ---------------------------------------------------------------------------

from opentelemetry import metrics as _otel_metrics
from opentelemetry.metrics import NoOpMeter as _NoOpMeter


class _InstrumentProxy:
    """Thin forwarder so callers that imported a module-level instrument
    name (via ``from horde.telemetry import _generate_duration``) keep
    working after ``_bind_instruments()`` swaps the underlying real
    instrument in post-configure."""

    __slots__ = ("_real",)

    def __init__(self, real):
        self._real = real

    def record(self, *args, **kwargs):  # histogram API
        return self._real.record(*args, **kwargs)

    def add(self, *args, **kwargs):  # counter API
        return self._real.add(*args, **kwargs)


_HISTOGRAM_SPECS = (
    ("_generate_duration", "horde.generate.duration", "s", "End-to-end duration of a generate request"),
    ("_pop_duration", "horde.pop.duration", "s", "End-to-end duration of a job_pop request"),
    ("_pop_query_duration", "horde.pop.wp_query.duration", "s", "Duration of get_sorted_wp_filtered_to_worker query"),
    ("_pop_candidates", "horde.pop.candidates_evaluated", "1", "Number of WaitingPrompts evaluated per pop"),
    ("_submit_duration", "horde.submit.duration", "s", "End-to-end duration of a job_submit request"),
    ("_submit_kudos", "horde.submit.kudos", "kudos", "Kudos awarded per job submission"),
    ("_ip_check_duration", "horde.countermeasures.ip_check.duration", "s", "Duration of is_ip_safe external check"),
    ("_job_duration", "horde.job.duration", "s", "Duration of a PrimaryTimedFunction invocation"),
    ("_webhook_duration", "horde.webhook.attempt.duration", "s", "Duration of a single webhook POST attempt"),
)

_COUNTER_SPECS = (
    ("_pop_skipped", "horde.pop.skipped", "1", "WPs skipped during pop, by reason"),
    ("_job_failures", "horde.job.failures", "1", "PrimaryTimedFunction invocations that raised"),
    ("_webhook_outcomes", "horde.webhook.outcomes", "1", "Terminal webhook outcomes, by reason (ok|http_error|exception|giveup)"),
)

# NoOp-backed proxies — populated with real instruments by _bind_instruments().
_noop_meter = _NoOpMeter("ai-horde")
for _name, _otel_name, _unit, _desc in _HISTOGRAM_SPECS:
    globals()[_name] = _InstrumentProxy(_noop_meter.create_histogram(_otel_name, unit=_unit, description=_desc))
for _name, _otel_name, _unit, _desc in _COUNTER_SPECS:
    globals()[_name] = _InstrumentProxy(_noop_meter.create_counter(_otel_name, unit=_unit, description=_desc))


def _bind_instruments():
    """Swap the NoOp instrument proxies with real ones from the post-configure
    meter. Must be called after ``logfire.configure()``.
    """
    meter = _otel_metrics.get_meter("ai-horde")
    for name, otel_name, unit, desc in _HISTOGRAM_SPECS:
        globals()[name]._real = meter.create_histogram(otel_name, unit=unit, description=desc)
    for name, otel_name, unit, desc in _COUNTER_SPECS:
        globals()[name]._real = meter.create_counter(otel_name, unit=unit, description=desc)


# ---------------------------------------------------------------------------
# Pyroscope low-cardinality tagging helper
# ---------------------------------------------------------------------------

def pyroscope_tag(**tags):
    """Context manager that applies low-cardinality Pyroscope tags (no-op if
    Pyroscope is unavailable).  Callers must only pass bounded tag keys/values
    (endpoint family, job type, etc.) — never raw user/worker IDs.
    """
    try:
        import pyroscope  # type: ignore
    except ImportError:
        from contextlib import nullcontext

        return nullcontext()
    return pyroscope.tag_wrapper(tags)
