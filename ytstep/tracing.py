"""Sends a trace of every message to Langfuse: each agent step, model call and
tool call, with their inputs and outputs. Used to inspect real chats and to
build eval datasets from them (see Metrics.md → "Traces and live evals").

Turned on only when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set
(plus LANGFUSE_BASE_URL for a non-EU region). Without them nothing is sent.
"""

import os

from .db import ON_VERCEL
from .observability import describe_error, get_logger, log

_log = get_logger("tracing")
_client = None


def environment() -> str:
    """production / preview on Vercel, local on your computer."""
    return os.environ.get("VERCEL_ENV", "development" if ON_VERCEL else "local")


def setup_tracing():
    """Starts Langfuse tracing once. Call before any agent runs."""
    global _client
    if _client is not None:
        return _client
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        log(_log, "tracing off (no LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")
        return None
    try:
        from langfuse import Langfuse
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor

        _client = Langfuse(
            environment=environment(),
            release=(os.environ.get("VERCEL_GIT_COMMIT_SHA") or "")[:7] or None,
        )
        GoogleADKInstrumentor().instrument()
        log(_log, "tracing on", destination=os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
            environment=environment())
    except Exception as err:  # noqa: BLE001 - tracing must never stop the app
        log(_log, "tracing could not start", error=describe_error(err))
        _client = None
    return _client


def current_trace_id() -> str | None:
    """The id of the trace being recorded right now (matches Langfuse's trace id)."""
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        return format(context.trace_id, "032x") if context.is_valid else None
    except Exception:  # noqa: BLE001
        return None


def flush() -> None:
    """Sends any traces still waiting. Vercel can pause the app right after a
    reply, so this runs at the end of every message."""
    if _client is not None:
        try:
            _client.flush()
        except Exception as err:  # noqa: BLE001
            log(_log, "could not send traces", error=describe_error(err))
