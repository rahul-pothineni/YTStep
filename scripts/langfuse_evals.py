"""Live evals with Langfuse: turn real chats into a dataset, then rerun that
dataset through the current agents and score the results. (Guide: Metrics.md)

    # 1. Copy recent live-site chats into a Langfuse dataset (needs .env.local
    #    from `vercel env pull .env.local`, and LANGFUSE_* keys in ytstep/.env)
    python scripts/langfuse_evals.py collect --days 7

    # 2. Rerun every chat in the dataset with the agents on this computer
    #    (e.g. after changing a prompt) and score each one
    python scripts/langfuse_evals.py run --name "shorter search queries"

    # Try the scoring without Langfuse on one made-up chat
    python scripts/langfuse_evals.py try "beginner yoga, 20 minutes, go search"

Each dataset item is one whole chat: every message the person sent, in order.
`run` replays those messages in a fresh session and scores the final answer.
The live site's own answer is kept in the item's metadata, so scores can
compare "now" against "what production said".
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("YTSTEP_METRICS", "off")  # replays shouldn't count as real traffic

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / "ytstep" / ".env")

DEFAULT_DATASET = "ytstep-production-chats"
JUDGE_MODEL = "gemini-3.5-flash-lite"
SPEAKERS = {"orchestrator", "query_eval_agent"}  # the agents whose text people see


# ---------- Reading chats ----------

def _texts(event: dict | object) -> str:
    content = getattr(event, "content", None)
    parts = (content.parts if content else None) or []
    return "".join(p.text or "" for p in parts if p.text and not p.thought).strip()


def chat_from_session(session) -> dict | None:
    """The person's messages, the last reply and the videos shown, from a saved chat."""
    messages, reply = [], ""
    for event in session.events:
        text = _texts(event)
        if not text:
            continue
        if event.author == "user":
            messages.append(text)
        elif event.author in SPEAKERS and not event.partial:
            reply = text
    if not messages:
        return None
    videos = [v.get("video_id") for v in session.state.get("relevant_videos") or []]
    return {"messages": messages, "reply": reply, "videos": videos,
            "refined_query": session.state.get("refined_query")}


async def collect(days: float, dataset: str, limit: int) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from google.adk.sessions import DatabaseSessionService
    from langfuse import get_client

    from ytstep.db import engine_options, postgres_url
    from ytstep.observability import _tables

    load_dotenv(REPO_ROOT / ".env.local", override=True)
    url = postgres_url()
    if not url:
        sys.exit("No DATABASE_URL in .env.local. Run: vercel env pull .env.local")

    _, runs, _ = _tables()
    engine = create_async_engine(url, **engine_options())
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(runs.c.session_id, runs.c.user_id, runs.c.trace_id, runs.c.started_at)
            .where(runs.c.started_at >= since, runs.c.environment == "production", runs.c.status != "running")
            .order_by(runs.c.started_at.desc())
        )).all()
    latest = {}  # newest run per chat → its trace links the dataset item to Langfuse
    for session_id, user_id, trace_id, _ in rows:
        latest.setdefault(session_id, (user_id, trace_id))

    sessions = DatabaseSessionService(db_engine=engine)
    langfuse = get_client()
    try:
        langfuse.create_dataset(name=dataset, description="Real YTStep chats from the live site")
    except Exception:  # noqa: BLE001 - already exists
        pass
    existing = {item.metadata.get("session_id") for item in langfuse.get_dataset(dataset).items
                if isinstance(item.metadata, dict)}

    added = 0
    for session_id, (user_id, trace_id) in list(latest.items())[:limit]:
        if session_id in existing:
            continue
        session = await sessions.get_session(app_name="ytstep", user_id=user_id, session_id=session_id)
        chat = chat_from_session(session) if session else None
        if not chat:
            continue
        langfuse.create_dataset_item(
            dataset_name=dataset,
            id=f"chat-{session_id}",  # adding the same chat twice updates it
            input={"messages": chat["messages"]},
            metadata={"session_id": session_id, "production_reply": chat["reply"],
                      "production_videos": chat["videos"], "production_request": chat["refined_query"]},
            source_trace_id=trace_id,
        )
        added += 1
    langfuse.flush()
    await engine.dispose()
    print(f"Added {added} chats to the '{dataset}' dataset ({len(existing)} were already there).")


# ---------- Replaying a chat ----------

async def replay(messages: list[str]) -> dict:
    """Runs a chat's messages through the agents on this computer. Returns what came out."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from ytstep.agent import app

    service = InMemorySessionService()
    runner = Runner(app=app, session_service=service)
    session = await service.create_session(app_name="ytstep", user_id="eval-replay")
    start, reply, tools, errors = time.perf_counter(), "", [], []
    for text in messages:
        async for event in runner.run_async(
            user_id="eval-replay", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
        ):
            if event.error_message:
                errors.append(event.error_message)
            for call in event.get_function_calls():
                tools.append(call.name)
            text_out = _texts(event)
            if text_out and event.author in SPEAKERS and not event.partial:
                reply = text_out
    session = await service.get_session(app_name="ytstep", user_id="eval-replay", session_id=session.id)
    return {
        "reply": reply,
        "videos": [{k: v.get(k) for k in ("video_id", "title", "duration", "summary")}
                   for v in session.state.get("relevant_videos") or []],
        "refined_query": session.state.get("refined_query"),
        "tools": tools,
        "errors": errors,
        "seconds": round(time.perf_counter() - start, 1),
    }


# ---------- Scores ----------

JUDGE_PROMPT = """You are grading a YouTube video finder. The person asked (in order):
{messages}

The finder understood the request as: {request}

It recommended these videos (title, length, summary):
{videos}

Its final message to the person:
{reply}

Score 0 to 1: how well do the recommended videos match what the person asked for
(topic, level, length, any other wishes)? If the finder asked a sensible clarifying
question instead of recommending videos because the request was too vague, score 1.
If it recommended nothing for a clear request, score 0.
Reply as JSON: {{"score": <0..1>, "reason": "<one sentence>"}}"""


def _judge(messages: list[str], output: dict) -> tuple[float, str]:
    from google import genai
    from google.genai import types

    videos = "\n".join(f"- {v['title']} ({v['duration']}): {str(v['summary'])[:300]}" for v in output["videos"]) or "(none)"
    prompt = JUDGE_PROMPT.format(messages="\n".join(f"- {m}" for m in messages),
                                 request=json.dumps(output["refined_query"]), videos=videos,
                                 reply=output["reply"][:1500])
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])  # keep a reference: it closes when dropped
    response = client.models.generate_content(
        model=JUDGE_MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    data = json.loads(response.text)
    return max(0.0, min(1.0, float(data["score"]))), str(data.get("reason", ""))


def score(messages: list[str], output: dict, metadata: dict | None = None) -> list[dict]:
    """The scores for one replayed chat. Same names show up as Langfuse scores."""
    metadata = metadata or {}
    found = len(output["videos"])
    scores = [
        {"name": "answered", "value": bool(output["reply"]) and not output["errors"],
         "comment": "; ".join(output["errors"])[:300] or None},
        {"name": "videos_found", "value": found},
        {"name": "seconds", "value": output["seconds"]},
    ]
    try:
        value, reason = _judge(messages, output)
        scores.append({"name": "relevance_judge", "value": value, "comment": reason})
    except Exception as err:  # noqa: BLE001
        scores.append({"name": "relevance_judge_failed", "value": True, "comment": str(err)[:300]})
    before = set(metadata.get("production_videos") or [])
    if before:
        now = {v["video_id"] for v in output["videos"]}
        scores.append({"name": "same_videos_as_production", "value": round(len(before & now) / len(before), 2)})
    return scores


# ---------- Running a dataset ----------

def run(dataset: str, name: str, concurrency: int) -> None:
    from langfuse import Evaluation, get_client

    from ytstep.tracing import setup_tracing

    if setup_tracing() is None:
        sys.exit("Langfuse isn't set up: add LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY to ytstep/.env")
    langfuse = get_client()

    async def task(*, item, **_):
        return await replay(item.input["messages"])

    def evaluator(*, input, output, metadata=None, **_):
        return [Evaluation(name=s["name"], value=s["value"], comment=s.get("comment"))
                for s in score(input["messages"], output, metadata)]

    def summary(*, item_results, **_):
        judged = [e.value for r in item_results for e in r.evaluations if e.name == "relevance_judge"]
        answered = [e.value for r in item_results for e in r.evaluations if e.name == "answered"]
        return [
            Evaluation(name="avg_relevance_judge", value=round(sum(judged) / len(judged), 3) if judged else 0),
            Evaluation(name="answer_rate", value=round(sum(answered) / len(answered), 3) if answered else 0),
        ]

    result = langfuse.get_dataset(dataset).run_experiment(
        name=name, description="Replay of real chats through the agents on this computer",
        task=task, evaluators=[evaluator], run_evaluators=[summary], max_concurrency=concurrency,
        metadata={"git": os.popen("git rev-parse --short HEAD").read().strip()},
    )
    print(result.format())
    langfuse.flush()


def try_one(messages: list[str]) -> None:
    output = asyncio.run(replay(messages))
    print(json.dumps({"output": {**output, "videos": [v["title"] for v in output["videos"]]},
                      "scores": score(messages, output)}, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="copy live-site chats into a Langfuse dataset")
    c.add_argument("--days", type=float, default=7)
    c.add_argument("--limit", type=int, default=50)
    c.add_argument("--dataset", default=DEFAULT_DATASET)
    r = sub.add_parser("run", help="rerun the dataset through the current agents and score it")
    r.add_argument("--name", required=True, help="what changed, e.g. 'new search prompt'")
    r.add_argument("--dataset", default=DEFAULT_DATASET)
    r.add_argument("--concurrency", type=int, default=3)
    t = sub.add_parser("try", help="replay and score one made-up chat (no Langfuse needed)")
    t.add_argument("messages", nargs="+", help="the person's messages, in order")
    args = parser.parse_args()

    if args.command == "collect":
        asyncio.run(collect(args.days, args.dataset, args.limit))
    elif args.command == "run":
        run(args.dataset, args.name, args.concurrency)
    else:
        try_one(args.messages)


if __name__ == "__main__":
    main()
