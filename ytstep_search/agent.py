from ytstep_testing import load_env

load_env()

from ytstep.agents import search_agent  # noqa: E402

root_agent = search_agent.clone()
