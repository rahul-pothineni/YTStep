from ytstep_testing import load_env

load_env()

from ytstep.agents import query_eval_agent  # noqa: E402

root_agent = query_eval_agent.clone()
