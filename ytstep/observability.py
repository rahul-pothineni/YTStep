"""Logging and metrics for YTStep (what each metric means: see Metrics.md).

Three pieces:
  * get_logger / log      one log line per thing that happens. Readable text on
                          your computer, JSON on Vercel (so its log search can
                          filter on fields like `invocation` or `ms`).
  * MetricsPlugin         an ADK plugin that times every message ("run"), agent,
                          model call and tool call, and saves them.
  * timed / map_in_threads
                          for tools: time a call to YouTube or Gemini and keep
                          the run's id on log lines from worker threads.

Metrics are saved to two tables, `metrics_runs` (one row per message) and
`metrics_spans` (one row per agent / model / tool / outside call). They go to
Postgres when DATABASE_URL is set (Vercel + Neon), otherwise to
ytstep/.adk/metrics.db. `python scripts/metrics_report.py` turns them into numbers.
Set YTSTEP_METRICS=off to stop saving them (logging still happens).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from google.adk.plugins.base_plugin import BasePlugin

from .db import LOCAL_DATA_DIR, ON_VERCEL, engine_options, postgres_url

# ---------- Logging ----------

_current_run: contextvars.ContextVar["_Run | None"] = contextvars.ContextVar("ytstep_run", default=None)
_configured = False


class _Formatter(logging.Formatter):
    def __init__(self, as_json: bool):
        super().__init__()
        self.as_json = as_json

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {}
        run = _current_run.get()
        if run:
            fields["invocation"] = run.invocation_id
        fields.update(getattr(record, "fields", {}))
        trace = self.formatException(record.exc_info) if record.exc_info else None

        if self.as_json:
            line = {
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "event": record.getMessage(),
                **fields,
            }
            if trace:
                line["trace"] = trace
            return json.dumps(line, default=str)

        stamp = datetime.now().strftime("%H:%M:%S")
        if "invocation" in fields:  # the end is enough to tell runs apart locally
            fields["invocation"] = str(fields["invocation"])[-8:]
        extras = " ".join(f"{k}={_short(v)}" for k, v in fields.items())
        text = f"{stamp} {record.levelname:<7} {record.name:<24} {record.getMessage()}  {extras}".rstrip()
        return f"{text}\n{trace}" if trace else text


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _configure() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    as_json = os.environ.get("LOG_FORMAT", "json" if ON_VERCEL else "text") == "json"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(as_json))
    root = logging.getLogger("ytstep")
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """A logger named `ytstep.<name>`, writing to the console (Vercel's logs when deployed)."""
    _configure()
    return logging.getLogger(f"ytstep.{name}")


def log(logger: logging.Logger, event: str, level: int = logging.INFO, exc_info: Any = None, **fields: Any) -> None:
    """Logs `event` with extra key=value fields, e.g. log(logger, "search done", ms=120, count=3)."""
    logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)


def describe_error(err: BaseException | str | None) -> str | None:
    if err is None:
        return None
    if isinstance(err, BaseException):
        return f"{type(err).__name__}: {str(err)[:400]}"
    return str(err)[:400]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


# ---------- Helpers for tools ----------

def record_span(kind: str, name: str, started_at: datetime, ms: float, status: str = "ok",
                error: str | None = None, details: dict | None = None, **numbers: Any) -> None:
    """Adds a timing to the current run's metrics (does nothing outside a run)."""
    run = _current_run.get()
    if run is None:
        return
    run.spans.append({
        "kind": kind, "name": name, "agent": run.turn.name if run.turn else None,
        "started_at": started_at, "duration_ms": ms, "status": status, "error": error,
        "details": details or None, **numbers,
    })


@contextlib.contextmanager
def timed(logger: logging.Logger, name: str, **fields: Any) -> Iterator[dict]:
    """Times a call to an outside service (YouTube, Gemini): logs it and records it.

        with timed(log, "youtube.search", query=q) as info:
            ...
            info["count"] = 5          # add fields to the log line
            info["status"] = "error"   # mark a failure that didn't raise
            info["error"] = "..."
    """
    start, started_at = time.perf_counter(), _now()
    info: dict[str, Any] = dict(fields)
    try:
        yield info
    except Exception as err:
        ms, error = _ms(start), describe_error(err)
        log(logger, f"{name} failed", logging.WARNING, ms=ms, error=error, **info)
        record_span("api", name, started_at, ms, "error", error, info)
        raise
    status = info.pop("status", "ok")
    error = info.pop("error", None)
    ms = _ms(start)
    log(logger, name if status == "ok" else f"{name} failed",
        logging.INFO if status == "ok" else logging.WARNING, ms=ms, status=status, error=error, **info)
    record_span("api", name, started_at, ms, status, describe_error(error), info)


def map_in_threads(fn: Callable[[Any], Any], items: list) -> list:
    """Like ThreadPoolExecutor.map, but each thread keeps the current run (for logs + metrics)."""
    if not items:
        return []
    parent = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        return list(pool.map(lambda item: parent.copy().run(fn, item), items))


# ---------- Where metrics are saved ----------

def _tables():
    from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, Text

    meta = MetaData()
    runs = Table(
        "metrics_runs", meta,
        Column("invocation_id", String(64), primary_key=True),
        Column("app_name", String(64)),
        Column("session_id", String(128), index=True),
        Column("user_id", String(128)),
        Column("trace_id", String(32)),  # the Langfuse trace for this message
        Column("environment", String(16)),  # production | preview | local
        Column("started_at", DateTime(timezone=True), index=True),
        Column("finished_at", DateTime(timezone=True)),
        Column("status", String(16)),  # running | ok | no_reply | error
        Column("error", Text),
        Column("duration_ms", Float),
        Column("first_reply_ms", Float),
        Column("agent_path", Text),
        Column("model_calls", Integer),
        Column("tool_calls", Integer),
        Column("input_tokens", Integer),
        Column("output_tokens", Integer),
        Column("relevance_retries", Integer),
        Column("videos_returned", Integer),
    )
    spans = Table(
        "metrics_spans", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("invocation_id", String(64), index=True),
        Column("kind", String(16)),  # agent | model | tool | api | page
        Column("name", String(128)),
        Column("agent", String(64)),
        Column("started_at", DateTime(timezone=True), index=True),
        Column("duration_ms", Float),
        Column("status", String(16)),
        Column("error", Text),
        Column("input_tokens", Integer),
        Column("output_tokens", Integer),
        Column("details", Text),
    )
    return meta, runs, spans


class MetricsStore:
    """Saves runs and spans. Never raises: metrics must not break the chat."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("YTSTEP_METRICS", "on").lower() != "off"
        self._engine = None
        self._tables = None
        self._lock: asyncio.Lock | None = None
        self._log = get_logger("metrics")

    def target(self) -> str:
        if not self.enabled:
            return "off"
        if postgres_url():
            return "postgres"
        return "none (no DATABASE_URL on Vercel)" if ON_VERCEL else "sqlite"

    async def _ready(self):
        if self._engine is not None:
            return self._tables
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            if self._engine is not None:
                return self._tables
            from sqlalchemy.ext.asyncio import create_async_engine

            url = postgres_url()
            if url:
                engine = create_async_engine(url, **engine_options())
            elif ON_VERCEL:
                return None  # nowhere lasting to write
            else:
                LOCAL_DATA_DIR.mkdir(exist_ok=True)
                engine = create_async_engine(f"sqlite+aiosqlite:///{LOCAL_DATA_DIR / 'metrics.db'}")
            meta, runs, spans = _tables()
            async with engine.begin() as conn:
                await conn.run_sync(meta.create_all)
            self._engine, self._tables = engine, (runs, spans)
            return self._tables

    async def _write(self, what: str, fn) -> None:
        if not self.enabled:
            return
        try:
            tables = await asyncio.wait_for(self._ready(), timeout=10)
            if tables is None:
                return
            async with self._engine.begin() as conn:
                await asyncio.wait_for(fn(conn, *tables), timeout=10)
        except Exception as err:  # noqa: BLE001
            log(self._log, f"could not save {what}", logging.WARNING, error=describe_error(err))

    async def run_started(self, row: dict) -> None:
        async def go(conn, runs, spans):
            await conn.execute(runs.insert().values(**row))
        await self._write("run start", go)

    async def run_finished(self, row: dict, span_rows: list[dict]) -> None:
        async def go(conn, runs, spans):
            key = row["invocation_id"]
            result = await conn.execute(runs.update().where(runs.c.invocation_id == key).values(**row))
            if result.rowcount == 0:
                await conn.execute(runs.insert().values(**row))
            if span_rows:
                await conn.execute(spans.insert(), span_rows)
        await self._write("run", go)

    async def add_spans(self, span_rows: list[dict]) -> None:
        rows = [{k: r.get(k) for k in SPAN_FIELDS} for r in span_rows]

        async def go(conn, runs, spans):
            await conn.execute(spans.insert(), rows)
        await self._write("spans", go)


store = MetricsStore()


# ---------- The plugin ----------

SPAN_FIELDS = ("invocation_id", "kind", "name", "agent", "started_at", "duration_ms", "status",
               "error", "input_tokens", "output_tokens", "details")


@dataclass
class _Turn:
    """One agent's turn: from when it starts until it finishes or hands off."""
    name: str
    start: float
    started_at: datetime


@dataclass
class _Run:
    invocation_id: str
    app_name: str
    session_id: str
    user_id: str
    start: float
    started_at: datetime
    trace_id: str | None = None
    environment: str = "local"
    turn: _Turn | None = None
    agent_path: list[str] = field(default_factory=list)
    spans: list[dict] = field(default_factory=list)
    model_starts: dict[str, list[tuple[float, datetime, str]]] = field(default_factory=dict)
    tool_starts: dict[str, tuple[float, datetime]] = field(default_factory=dict)
    first_reply_ms: float | None = None
    replied: bool = False
    error: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    start_write: asyncio.Task | None = None


def _brief(value: Any) -> Any:
    """Small things from a tool's arguments/result, for logs (no long text)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, (int, float, bool)) or v is None:
                out[k] = v
            elif isinstance(v, str):
                out[k] = _short(v, 120)
            elif isinstance(v, (list, dict)):
                out[k] = f"<{len(v)} items>"
        return out
    return _short(value, 120)


class MetricsPlugin(BasePlugin):
    """Logs and times everything that happens while YTStep answers a message."""

    def __init__(self) -> None:
        super().__init__(name="ytstep_metrics")
        self._runs: dict[str, _Run] = {}
        self._log = get_logger("run")

    def _run(self, invocation_id: str) -> _Run | None:
        return self._runs.get(invocation_id)

    # -- the whole message --

    async def before_run_callback(self, *, invocation_context):
        from .tracing import current_trace_id, environment

        session = invocation_context.session
        run = _Run(
            invocation_id=invocation_context.invocation_id,
            app_name=session.app_name, session_id=session.id, user_id=session.user_id,
            start=time.perf_counter(), started_at=_now(),
            trace_id=current_trace_id(), environment=environment(),
        )
        self._runs[run.invocation_id] = run
        _current_run.set(run)
        text = "".join(p.text or "" for p in (invocation_context.user_content.parts or [])) \
            if invocation_context.user_content else ""
        log(self._log, "message received", session=run.session_id, user=run.user_id,
            chars=len(text), trace=run.trace_id)
        run.start_write = asyncio.create_task(store.run_started({
            "invocation_id": run.invocation_id, "app_name": run.app_name, "session_id": run.session_id,
            "user_id": run.user_id, "trace_id": run.trace_id, "environment": run.environment,
            "started_at": run.started_at, "status": "running",
        }))
        return None

    async def on_event_callback(self, *, invocation_context, event):
        run = self._run(invocation_context.invocation_id)
        if run is None:
            return None
        if event.error_code or event.error_message:
            run.error = run.error or describe_error(f"{event.error_code}: {event.error_message}")
            log(self._log, "error event", logging.ERROR, author=event.author,
                code=event.error_code, error=event.error_message)
        parts = (event.content.parts if event.content else None) or []
        if event.author != "user" and not event.partial and any(p.text and not p.thought for p in parts):
            run.replied = True
            if run.first_reply_ms is None:
                run.first_reply_ms = _ms(run.start)
                log(self._log, "first reply sent", agent=event.author, ms=run.first_reply_ms)
        return None

    async def after_run_callback(self, *, invocation_context):
        await self._finish(invocation_context, None)

    async def on_run_error_callback(self, *, invocation_context, error):
        await self._finish(invocation_context, error)

    async def _finish(self, invocation_context, error: BaseException | None) -> None:
        run = self._runs.pop(invocation_context.invocation_id, None)
        if run is None:
            return
        if error is not None:
            run.error = describe_error(error)
        if run.turn:  # the last agent: finished normally unless the run broke
            self._end_turn(run, "error" if run.error else "ok", run.error)

        state = invocation_context.session.state
        checked = "relevance_check_agent" in run.agent_path
        status = "error" if run.error else ("ok" if run.replied else "no_reply")
        row = {
            "invocation_id": run.invocation_id, "app_name": run.app_name, "session_id": run.session_id,
            "user_id": run.user_id, "trace_id": run.trace_id, "environment": run.environment,
            "started_at": run.started_at, "finished_at": _now(),
            "status": status, "error": run.error, "duration_ms": _ms(run.start),
            "first_reply_ms": run.first_reply_ms, "agent_path": " > ".join(run.agent_path),
            "model_calls": run.model_calls, "tool_calls": run.tool_calls,
            "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
            "relevance_retries": state.get("relevance_retry_count") if checked else None,
            "videos_returned": len(state.get("relevant_videos") or []) if checked else None,
        }
        log(self._log, "message finished",
            logging.INFO if status == "ok" else logging.ERROR,
            status=status, ms=row["duration_ms"], first_reply_ms=run.first_reply_ms,
            agents=row["agent_path"], model_calls=run.model_calls, tool_calls=run.tool_calls,
            tokens_in=run.input_tokens, tokens_out=run.output_tokens, error=run.error)

        spans = [{**{k: s.get(k) for k in SPAN_FIELDS}, "invocation_id": run.invocation_id,
                  "details": json.dumps(s["details"], default=str) if s.get("details") else None}
                 for s in run.spans]
        if run.start_write:
            with contextlib.suppress(Exception):
                await run.start_write
        await store.run_finished(row, spans)
        from .tracing import flush

        await asyncio.to_thread(flush)
        _current_run.set(None)

    # -- agents --
    # ADK runs one agent at a time here: each hands off to the next with
    # transfer_to_agent. An agent that hands off never gets an "after agent"
    # callback, so a turn ends when the next agent starts (or the run ends).

    async def before_agent_callback(self, *, agent, callback_context):
        run = self._run(callback_context.invocation_id)
        if run is not None:
            if run.turn:
                self._end_turn(run, "ok", None)
            run.turn = _Turn(agent.name, time.perf_counter(), _now())
            run.agent_path.append(agent.name)
            log(self._log, "agent started", agent=agent.name)
        return None

    async def after_agent_callback(self, *, agent, callback_context):
        run = self._run(callback_context.invocation_id)
        if run is not None and run.turn and run.turn.name == agent.name:
            self._end_turn(run, "ok", None)
        return None

    async def on_agent_error_callback(self, *, agent, callback_context, error):
        run = self._run(callback_context.invocation_id)
        if run is not None and run.turn and run.turn.name == agent.name:
            self._end_turn(run, "error", describe_error(error))
        return None

    def _end_turn(self, run: _Run, status: str, error: str | None) -> None:
        turn, run.turn = run.turn, None
        ms = _ms(turn.start)
        run.spans.append({"kind": "agent", "name": turn.name, "agent": turn.name, "started_at": turn.started_at,
                          "duration_ms": ms, "status": status, "error": error})
        log(self._log, "agent finished", logging.INFO if status == "ok" else logging.ERROR,
            agent=turn.name, ms=ms, status=status, error=error)

    # -- model calls --

    async def before_model_callback(self, *, callback_context, llm_request):
        run = self._run(callback_context.invocation_id)
        if run is not None:
            run.model_starts.setdefault(callback_context.agent_name, []).append(
                (time.perf_counter(), _now(), llm_request.model or "?"))
        return None

    def _close_model(self, callback_context) -> tuple[_Run, float, datetime, str] | None:
        run = self._run(callback_context.invocation_id)
        starts = run.model_starts.get(callback_context.agent_name) if run else None
        if not starts:
            return None
        start, started_at, model = starts.pop()
        run.model_calls += 1
        return run, _ms(start), started_at, model

    async def after_model_callback(self, *, callback_context, llm_response):
        if llm_response.partial:  # streaming piece; wait for the full answer
            return None
        closed = self._close_model(callback_context)
        if closed is None:
            return None
        run, ms, started_at, model = closed
        usage = llm_response.usage_metadata
        tokens_in = (usage.prompt_token_count or 0) if usage else 0
        tokens_out = ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0
        run.input_tokens += tokens_in
        run.output_tokens += tokens_out
        parts = (llm_response.content.parts if llm_response.content else None) or []
        calls = [p.function_call.name for p in parts if p.function_call]
        status = "error" if llm_response.error_code else "ok"
        error = describe_error(f"{llm_response.error_code}: {llm_response.error_message}") if status == "error" else None
        run.spans.append({"kind": "model", "name": model, "agent": callback_context.agent_name,
                          "started_at": started_at, "duration_ms": ms, "status": status, "error": error,
                          "input_tokens": tokens_in, "output_tokens": tokens_out,
                          "details": {"tool_calls": calls} if calls else None})
        log(self._log, "model call", logging.INFO if status == "ok" else logging.ERROR,
            agent=callback_context.agent_name, model=model, ms=ms, tokens_in=tokens_in,
            tokens_out=tokens_out, calls=calls or None, error=error)
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        closed = self._close_model(callback_context)
        if closed is not None:
            run, ms, started_at, model = closed
            run.spans.append({"kind": "model", "name": model, "agent": callback_context.agent_name,
                              "started_at": started_at, "duration_ms": ms, "status": "error",
                              "error": describe_error(error)})
        log(self._log, "model call failed", logging.ERROR, agent=callback_context.agent_name,
            model=llm_request.model, error=describe_error(error))
        return None

    # -- tool calls --

    def _tool_key(self, tool, tool_context) -> str:
        return tool_context.function_call_id or tool.name

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        run = self._run(tool_context.invocation_id)
        if run is not None:
            run.tool_starts[self._tool_key(tool, tool_context)] = (time.perf_counter(), _now())
            log(self._log, "tool started", tool=tool.name, args=_brief(tool_args))
        return None

    def _close_tool(self, tool, tool_context, status: str, error: str | None, result: Any = None) -> None:
        run = self._run(tool_context.invocation_id)
        started = run.tool_starts.pop(self._tool_key(tool, tool_context), None) if run else None
        if started is None:
            return
        run.tool_calls += 1
        ms = _ms(started[0])
        run.spans.append({"kind": "tool", "name": tool.name, "agent": tool_context.agent_name,
                          "started_at": started[1], "duration_ms": ms, "status": status, "error": error})
        log(self._log, "tool finished" if status == "ok" else "tool failed",
            logging.INFO if status == "ok" else logging.ERROR,
            tool=tool.name, ms=ms, status=status, error=error,
            result=_brief(result) if result is not None and status == "ok" else None)

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        # Our tools report problems by returning {"error": "..."} instead of raising.
        error = result.get("error") if isinstance(result, dict) else None
        self._close_tool(tool, tool_context, "error" if error else "ok", describe_error(error), result)
        return None

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        self._close_tool(tool, tool_context, "error", describe_error(error))
        return None
