"""Shared setup for the single-agent test apps (ytstep_*/) shown in `adk web`."""

import copy
import json
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    # adk web only looks for .env in the app folder and its parents, so ytstep/.env isn't found from a sibling folder.
    load_dotenv(REPO_ROOT / "ytstep" / ".env")


def seed_state_from(path: Path):
    """Return a before_agent_callback that fills missing session state from a JSON file."""

    def seed_state(callback_context):
        sample = json.loads(Path(path).read_text())
        for key, value in sample.items():
            if key not in callback_context.state:
                callback_context.state[key] = copy.deepcopy(value)
        return None

    return seed_state
