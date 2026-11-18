"""Prints YTStep's metrics (definitions: Metrics.md).

    python scripts/metrics_report.py                 # this computer (ytstep/.adk/metrics.db)
    python scripts/metrics_report.py --prod          # the live site (Neon; needs .env.local
                                                     #   from `vercel env pull .env.local`)
    python scripts/metrics_report.py --prod --days 1 --env production
"""

import argparse
import asyncio
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

STUCK_AFTER = timedelta(minutes=6)  # Vercel stops a request at 5 minutes


def pct(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p50 = median, p95 = 95% were faster)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def secs(ms: float | None) -> str:
    return "-" if ms is None else (f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms")


def rate(part: int, whole: int) -> str:
    return "-" if not whole else f"{100 * part / whole:.1f}%"


def table(title: str, headers: list[str], rows: list[list]) -> None:
    print(f"\n### {title}\n")
    if not rows:
        print("_no data yet_")
        return
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        print("| " + " | ".join(str(c) for c in row) + " |")


def timing_rows(groups: dict[str, list[dict]]) -> list[list]:
    rows = []
    for name, items in sorted(groups.items(), key=lambda kv: -sum(i["duration_ms"] or 0 for i in kv[1])):
        times = [i["duration_ms"] for i in items if i["duration_ms"] is not None]
        failed = sum(1 for i in items if i["status"] != "ok")
        rows.append([name, len(items), secs(pct(times, 50)), secs(pct(times, 95)), secs(max(times, default=None)),
                     f"{failed} ({rate(failed, len(items))})"])
    return rows


async def load(url: str, since: datetime, env: str | None) -> tuple[list[dict], list[dict]]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from ytstep.db import engine_options
    from ytstep.observability import _tables

    _, runs, spans = _tables()
    engine = create_async_engine(url, **(engine_options() if url.startswith("postgresql") else {}))
    try:
        async with engine.connect() as conn:
            q = select(runs).where(runs.c.started_at >= since)
            if env:
                q = q.where(runs.c.environment == env)
            run_rows = [dict(r._mapping) for r in await conn.execute(q)]
            span_rows = [dict(r._mapping) for r in await conn.execute(select(spans).where(spans.c.started_at >= since))]
    finally:
        await engine.dispose()
    ids = {r["invocation_id"] for r in run_rows}
    span_rows = [s for s in span_rows if s["invocation_id"] in ids or (s["kind"] == "page" and not env)]
    return run_rows, span_rows


def report(runs: list[dict], spans: list[dict], days: float, where: str) -> None:
    now = datetime.now(timezone.utc)

    def aware(dt):
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    for r in runs:
        r["started_at"] = aware(r["started_at"])
        if r["status"] == "running" and now - r["started_at"] > STUCK_AFTER:
            r["status"] = "cut_off"  # started but never finished: the server was stopped mid-message
    finished = [r for r in runs if r["status"] != "running"]
    by_status = defaultdict(int)
    for r in finished:
        by_status[r["status"]] += 1
    failed = sum(n for s, n in by_status.items() if s != "ok")
    tool_failures = {s["invocation_id"] for s in spans if s["kind"] == "tool" and s["status"] != "ok"}
    degraded = [r for r in finished if r["status"] == "ok" and r["invocation_id"] in tool_failures]

    print(f"## YTStep metrics: last {days:g} days, {where} (generated {now:%Y-%m-%d %H:%M} UTC)")

    table("Messages (server side)", ["Metric", "Value"], [
        ["Messages answered or failed", len(finished)],
        ["Still running", len(runs) - len(finished)],
        ["**Message failure rate**", f"**{rate(failed, len(finished))}** ({failed})"],
        ["… errors (something broke)", by_status.get("error", 0)],
        ["… no reply (finished without saying anything)", by_status.get("no_reply", 0)],
        ["… cut off (never finished, e.g. 5-minute limit)", by_status.get("cut_off", 0)],
        ["Degraded: answered, but a tool failed along the way",
         f"{len(degraded)} ({rate(len(degraded), by_status.get('ok', 0))} of answered)"],
    ])

    page = [s for s in spans if s["kind"] == "page"]
    page_status = defaultdict(int)
    for s in page:
        page_status[s["status"]] += 1
    page_bad = len(page) - page_status.get("ok", 0)
    page_times = [s["duration_ms"] for s in page if s["status"] == "ok"]
    table("Messages (what people saw, reported by the page)", ["Metric", "Value"], [
        ["Messages reported", len(page)],
        ["**Failure rate seen by people**", f"**{rate(page_bad, len(page))}** ({page_bad})"],
        ["… no answer / error / offline",
         f"{page_status.get('no_answer', 0)} / {page_status.get('error', 0)} / {page_status.get('offline', 0)}"],
        ["Wait for an answer p50 / p95", f"{secs(pct(page_times, 50))} / {secs(pct(page_times, 95))}"],
    ] if page else [])

    ok = [r for r in finished if r["status"] == "ok"]
    searches = [r for r in ok if r["videos_returned"] is not None]
    quick = [r for r in ok if r["videos_returned"] is None]
    rows = []
    for label, group in (("All answered messages", ok), ("Full searches (reached the relevance check)", searches),
                         ("Quick replies (questions, chat)", quick)):
        t = [r["duration_ms"] for r in group if r["duration_ms"] is not None]
        rows.append([label, len(group), secs(pct(t, 50)), secs(pct(t, 90)), secs(pct(t, 95)), secs(max(t, default=None))])
    first = [r["first_reply_ms"] for r in ok if r["first_reply_ms"] is not None]
    rows.append(["Time to first reply text", len(first), secs(pct(first, 50)), secs(pct(first, 90)),
                 secs(pct(first, 95)), secs(max(first, default=None))])
    table("Response time per message (end to end, server side)", ["", "Count", "p50", "p90", "p95", "Max"], rows)

    run_ids = {r["invocation_id"] for r in finished}
    agent_spans = [s for s in spans if s["kind"] == "agent" and s["invocation_id"] in run_ids]
    turns = defaultdict(list)
    per_message = defaultdict(lambda: defaultdict(float))
    for s in agent_spans:
        turns[s["name"]].append(s)
        per_message[s["name"]][s["invocation_id"]] += s["duration_ms"] or 0
    table("Response time per agent turn", ["Agent", "Turns", "p50", "p95", "Max", "Failed"], timing_rows(turns))
    table("Time per agent per message (all its turns added up)", ["Agent", "Messages", "p50", "p95", "Share of total time"], [
        [name, len(v), secs(pct(list(v.values()), 50)), secs(pct(list(v.values()), 95)),
         rate(sum(v.values()), sum(sum(x.values()) for x in per_message.values()))]
        for name, v in sorted(per_message.items(), key=lambda kv: -sum(kv[1].values()))
    ])

    tools = defaultdict(list)
    for s in spans:
        if s["kind"] == "tool" and s["name"] != "transfer_to_agent":
            tools[s["name"]].append(s)
    table("Tool calls", ["Tool", "Calls", "p50", "p95", "Max", "Failed"], timing_rows(tools))

    models = defaultdict(list)
    for s in spans:
        if s["kind"] == "model":
            models[f"{s['agent']} ({s['name']})"].append(s)
    rows = timing_rows(models)
    for row in rows:
        items = models[row[0]]
        row.append(f"{sum(i['input_tokens'] or 0 for i in items) / len(items):,.0f} / "
                   f"{sum(i['output_tokens'] or 0 for i in items) / len(items):,.0f}")
    table("Model calls (per agent)", ["Agent (model)", "Calls", "p50", "p95", "Max", "Failed", "Avg tokens in / out"], rows)

    api = defaultdict(list)
    for s in spans:
        if s["kind"] == "api":
            api[s["name"]].append(s)
    table("Outside services (YouTube, Gemini video + comments)", ["Call", "Calls", "p50", "p95", "Max", "Failed"],
          timing_rows(api))

    def avg(values):
        values = [v for v in values if v is not None]
        return f"{sum(values) / len(values):,.1f}" if values else "-"

    table("Pipeline", ["Metric", "Value"], [
        ["Model calls per message (avg)", avg([r["model_calls"] for r in ok])],
        ["Tool calls per message (avg)", avg([r["tool_calls"] for r in ok])],
        ["Tokens in / out per message (avg)", f"{avg([r['input_tokens'] for r in ok])} / {avg([r['output_tokens'] for r in ok])}"],
        ["Relevance retries per search (avg)", avg([r["relevance_retries"] for r in searches])],
        ["Videos returned per search (avg)", avg([r["videos_returned"] for r in searches])],
        ["Searches that returned no videos", f"{sum(1 for r in searches if not r['videos_returned'])} "
                                             f"({rate(sum(1 for r in searches if not r['videos_returned']), len(searches))})"],
    ])

    errors = defaultdict(int)
    for r in finished:
        if r["error"]:
            errors[r["error"][:120]] += 1
    for s in spans:
        if s["status"] not in ("ok",) and s["error"]:
            errors[f"[{s['kind']}: {s['name']}] {s['error'][:100]}"] += 1
    table("Most common errors", ["Error", "Count"], sorted(([e, n] for e, n in errors.items()), key=lambda x: -x[1])[:10])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prod", action="store_true", help="read the live site's metrics (Neon) via .env.local")
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--env", choices=["production", "preview", "development", "local"], help="only this environment")
    args = parser.parse_args()

    if args.prod:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env.local", override=True)
        from ytstep.db import postgres_url

        url = postgres_url()
        if not url:
            sys.exit("No DATABASE_URL in .env.local. Run: vercel env pull .env.local")
        where = "live site (Neon)"
    else:
        db = REPO_ROOT / "ytstep" / ".adk" / "metrics.db"
        if not db.exists():
            sys.exit("No local metrics yet: chat with YTStep locally first (python frontend/server.py or adk web).")
        url, where = f"sqlite+aiosqlite:///{db}", "this computer"

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    runs, spans = asyncio.run(load(url, since, args.env))
    report(runs, spans, args.days, where + (f", {args.env}" if args.env else ""))


if __name__ == "__main__":
    main()
