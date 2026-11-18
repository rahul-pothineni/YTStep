"""YTStep chat page + ADK API in one server.

Locally (from the repo root):  python frontend/server.py  →  http://127.0.0.1:8080
On Vercel: Vercel loads `app` from this file (see pyproject.toml [tool.vercel]).

Settings come from environment variables:
  GOOGLE_API_KEY, YOUTUBE_API_KEY   the agents' keys (locally: ytstep/.env)
  DATABASE_URL_UNPOOLED / DATABASE_URL
                                    Postgres for saved chats (set by Vercel's Neon
                                    integration). Without one, chats are saved to
                                    ytstep/.adk/session.db like `adk web` does.
  APP_PASSCODE                      if set, people must enter this code before
                                    they can chat. Leave unset to skip the screen.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import uvicorn
from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.adk.cli.fast_api import get_fast_api_app
from google.genai import types
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
HOST, PORT = "127.0.0.1", 8080
ON_VERCEL = bool(os.environ.get("VERCEL"))
APP_NAME = "ytstep"  # the only agent app this server lets people run

load_dotenv(REPO_ROOT / "ytstep" / ".env")
PASSCODE = os.environ.get("APP_PASSCODE", "").strip()


# ---------- Where chats are saved ----------

def _postgres_url() -> str | None:
    """Turns the Postgres address Neon/Vercel gives us into one ADK can use.

    ADK needs the async `asyncpg` driver named in the address, and asyncpg
    doesn't understand the `sslmode`/`channel_binding` options in Neon's
    address, so those are dropped and SSL is turned on separately below.
    """
    url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    parts = urlsplit(url)
    return urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, "", ""))


DB_URL = _postgres_url()
if ON_VERCEL and not DB_URL:
    print("[ytstep] WARNING: no DATABASE_URL set, so chats are only kept in memory "
          "and will disappear. Add a Neon Postgres database to the Vercel project.")
DB_OPTIONS = None
if DB_URL:
    from sqlalchemy.pool import NullPool

    DB_OPTIONS = {
        # Serverless functions freeze between requests, so don't keep idle connections.
        "poolclass": NullPool,
        "connect_args": {"ssl": "require", "statement_cache_size": 0},
    }

# ADK's own API server (the same one behind `adk web`), without its dev UI.
app = get_fast_api_app(
    agents_dir=str(REPO_ROOT),
    web=False,
    session_service_uri=DB_URL,
    session_db_kwargs=DB_OPTIONS,
    # Vercel's disk is read-only, so nothing may be written next to the code.
    use_local_storage=not ON_VERCEL,
    # Locally, only answer requests addressed to this computer. Vercel handles this itself.
    bind_host=None if ON_VERCEL else HOST,
    port=PORT,
)


# ---------- Passcode ----------

def _unlock_token() -> str:
    # Changing APP_PASSCODE changes the token, which signs everyone out.
    return hmac.new(PASSCODE.encode(), b"ytstep-unlock", hashlib.sha256).hexdigest()


def _is_unlocked(cookies: dict[str, str]) -> bool:
    return not PASSCODE or hmac.compare_digest(cookies.get("ytstep_pass", ""), _unlock_token())


class UnlockRequest(BaseModel):
    code: str


@app.get("/api/unlock")
async def unlock_status(request: Request) -> dict:
    return {"locked": not _is_unlocked(request.cookies)}


@app.post("/api/unlock")
async def unlock(req: UnlockRequest) -> JSONResponse:
    if not PASSCODE:
        return JSONResponse({"ok": True})
    if not hmac.compare_digest(req.code.strip().encode(), PASSCODE.encode()):
        await asyncio.sleep(1)  # slows down guessing
        return JSONResponse({"ok": False}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "ytstep_pass", _unlock_token(),
        max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=ON_VERCEL,
    )
    return response


# ---------- Chat titles for the sidebar ----------

TITLE_MODEL = "gemini-3.5-flash-lite"
TITLE_PROMPT = (
    "Someone opened a chat to find YouTube videos. Write a 2 to 5 word title for the chat "
    "that names the topic they want to learn about or watch, based on their first message "
    "(or first few messages, if the first one is only a greeting). "
    "Fix any spelling mistakes. Don't use the words video, YouTube, watch, find or search. "
    "If there's no topic yet, reply with: New Search. "
    "Title Case, no quotes, no emoji, no ending punctuation. Reply with the title only.\n\n"
    "Their messages:\n"
)


class TitleRequest(BaseModel):
    text: str


def _fallback_title(text: str) -> str:
    words = text.split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "") or "New Search"


@app.post("/api/chat-title")
async def chat_title(req: TitleRequest) -> dict:
    """Names a chat from its first message. Falls back to the message's first words."""
    try:
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=TITLE_MODEL,
                contents=TITLE_PROMPT + req.text[:1000],
                config=types.GenerateContentConfig(max_output_tokens=20, temperature=0.2),
            ),
            timeout=5,
        )
        title = (response.text or "").strip().strip("\"'").rstrip(".!?")
        return {"title": title[:60] or _fallback_title(req.text)}
    except Exception as err:  # Title is a nice-to-have: never block the chat on it.
        print(f"[chat-title] falling back: {err}")
        return {"title": _fallback_title(req.text)}


# ---------- Browser id, half of it kept in the browser's cache ----------
# Each call makes a new random code, but the browser caches the answer for a
# year and reuses it. Emptying the cache throws the code away, so the page gets
# a new one and starts with an empty chat history.

@app.get("/api/cache-key")
async def cache_key() -> JSONResponse:
    return JSONResponse(
        {"key": uuid.uuid4().hex},
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# ---------- Gatekeeper: only what the chat page needs gets through ----------
# ADK's server has many more routes (other agent apps, API docs, live audio,
# memory, ...). On a public address those would let anyone run any agent on our
# API keys, so everything not listed here gets a 404.

OPEN = {("GET", "/api/unlock"), ("POST", "/api/unlock"), ("GET", "/api/cache-key"), ("GET", "/health")}
SESSIONS = re.compile(rf"^/apps/{APP_NAME}/users/browser-[\w-]{{1,80}}/sessions(/[\w-]{{1,80}})?$")


def _is_page_file(path: str) -> bool:
    if path == "/":
        return True
    target = (STATIC_DIR / path.lstrip("/")).resolve()
    return target.is_file() and STATIC_DIR in target.parents


def _route_kind(method: str, path: str) -> str | None:
    """'open' (no passcode needed), 'locked' (needs passcode), or None (blocked)."""
    if (method, path) in OPEN or (method in ("GET", "HEAD") and _is_page_file(path)):
        return "open"
    if (method, path) in {("POST", "/api/chat-title"), ("POST", "/run_sse")}:
        return "locked"
    match = SESSIONS.match(path)
    if match and method in (("GET", "PATCH") if match.group(1) else ("GET", "POST")):
        return "locked"
    return None


class Gatekeeper:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":  # live audio isn't used by the page
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        kind = _route_kind(scope["method"], scope["path"])
        if kind is None:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        if kind == "locked" and not _is_unlocked(Request(scope).cookies):
            await JSONResponse({"detail": "Passcode needed"}, status_code=401)(scope, receive, send)
            return

        if scope["path"] == "/run_sse":
            # Read the request to check it runs our app for a browser user, then hand it on.
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            try:
                data = json.loads(body)
                allowed = (data.get("appName") or data.get("app_name")) == APP_NAME and str(
                    data.get("userId") or data.get("user_id") or ""
                ).startswith("browser-")
            except ValueError:
                allowed = False
            if not allowed:
                await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
                return
            receive = _replay(body, receive)
        await self.app(scope, receive, send)


def _replay(body: bytes, receive):
    """Gives ADK the request body we already read, then carries on as normal."""
    sent = False

    async def replayed():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replayed


app.add_middleware(Gatekeeper)


# The chat page. Mounted last so the routes above (/run_sse, /apps/..., /api/...) win.
# On Vercel these files are served straight from Vercel's CDN.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="chat")

if __name__ == "__main__":
    print(f"\n  YTStep is running at http://{HOST}:{PORT}\n")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
