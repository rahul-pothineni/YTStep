from pathlib import Path

from ytstep_testing import load_env, seed_state_from

load_env()

from ytstep.agents import video_summarizer_agent  # noqa: E402

root_agent = video_summarizer_agent.clone(
    update={"before_agent_callback": seed_state_from(Path(__file__).parent / "sample_state.json")}
)
