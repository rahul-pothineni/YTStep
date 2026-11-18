"""Where YTStep keeps data: Postgres on Vercel (Neon), a local file otherwise.

Used by the chat server (saved chats) and by the metrics recorder.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ON_VERCEL = bool(os.environ.get("VERCEL"))
LOCAL_DATA_DIR = Path(__file__).resolve().parent / ".adk"


def postgres_url() -> str | None:
    """Turns the Postgres address Neon/Vercel gives us into one SQLAlchemy can use.

    It needs the async `asyncpg` driver named in the address, and asyncpg
    doesn't understand the `sslmode`/`channel_binding` options in Neon's
    address, so those are dropped and SSL is turned on in `engine_options`.
    """
    url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    parts = urlsplit(url)
    return urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, "", ""))


def engine_options() -> dict:
    """Connection settings for Postgres on serverless (Vercel)."""
    from sqlalchemy.pool import NullPool

    return {
        # Serverless functions freeze between requests, so don't keep idle connections.
        "poolclass": NullPool,
        "connect_args": {"ssl": "require", "statement_cache_size": 0},
    }
