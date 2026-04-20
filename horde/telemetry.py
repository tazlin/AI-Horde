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
# We acquire instruments from the OTel global meter API (not logfire.metric_*)
# because the OTel global uses a ProxyMeterProvider that correctly rewires
# instruments to the real exporter after `logfire.configure()` runs. Using
# `logfire.metric_histogram(...)` at module import time (before configure)
# binds to logfire's pre-configure state and the subsequent reconfiguration
# does not retroactively hook those instruments to the OTLP exporter, which
# caused our custom `horde.*` histograms to silently not export.
# ---------------------------------------------------------------------------

from opentelemetry import metrics as _otel_metrics

_meter = _otel_metrics.get_meter("ai-horde")

_generate_duration = _meter.create_histogram(
    "horde.generate.duration",
    unit="s",
    description="End-to-end duration of a generate request",
)

_pop_duration = _meter.create_histogram(
    "horde.pop.duration",
    unit="s",
    description="End-to-end duration of a job_pop request",
)

_pop_query_duration = _meter.create_histogram(
    "horde.pop.wp_query.duration",
    unit="s",
    description="Duration of get_sorted_wp_filtered_to_worker query",
)

_pop_candidates = _meter.create_histogram(
    "horde.pop.candidates_evaluated",
    unit="1",
    description="Number of WaitingPrompts evaluated per pop",
)

_pop_skipped = _meter.create_counter(
    "horde.pop.skipped",
    unit="1",
    description="WPs skipped during pop, by reason",
)

_submit_duration = _meter.create_histogram(
    "horde.submit.duration",
    unit="s",
    description="End-to-end duration of a job_submit request",
)

_submit_kudos = _meter.create_histogram(
    "horde.submit.kudos",
    unit="kudos",
    description="Kudos awarded per job submission",
)

_ip_check_duration = _meter.create_histogram(
    "horde.countermeasures.ip_check.duration",
    unit="s",
    description="Duration of is_ip_safe external check",
)

# Background timed jobs (PrimaryTimedFunction) — observability for queue pruning,
# stats, filter regex rebuilds, monthly kudos, etc.
_job_duration = _meter.create_histogram(
    "horde.job.duration",
    unit="s",
    description="Duration of a PrimaryTimedFunction invocation",
)

_job_failures = _meter.create_counter(
    "horde.job.failures",
    unit="1",
    description="PrimaryTimedFunction invocations that raised",
)

# Outbound webhooks — record per-attempt latency and terminal outcomes so
# webhook reliability can be dashboarded and alerted on independently of
# the (already-instrumented) surrounding span.
_webhook_duration = _meter.create_histogram(
    "horde.webhook.attempt.duration",
    unit="s",
    description="Duration of a single webhook POST attempt",
)

_webhook_outcomes = _meter.create_counter(
    "horde.webhook.outcomes",
    unit="1",
    description="Terminal webhook outcomes, by reason (ok|http_error|exception|giveup)",
)


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
