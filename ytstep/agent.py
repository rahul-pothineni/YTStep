from .tracing import setup_tracing

setup_tracing()  # before the agents load, so every step gets traced (Langfuse)

from google.adk.agents import Agent  # noqa: E402
from google.adk.apps import App  # noqa: E402

from .observability import MetricsPlugin  # noqa: E402

from .agents import (  # noqa: E402
    query_eval_agent,
    relevance_check_agent,
    search_agent,
    video_summarizer_agent,
)

root_agent = Agent(
    name="orchestrator",
    model="gemini-3.5-flash-lite",
    instruction="""\
You're the main coordinator for YTStep — a tool that helps people find the right
YouTube videos. Talk like a normal, friendly person. No tech speak.

When someone asks you to find a video:
1. Hand things off to query_eval_agent so they can figure out exactly what the
   user is looking for.
2. When query_eval_agent is done and comes back, immediately transfer to
   search_agent to search YouTube.
3. When search_agent comes back with results, immediately transfer to
   video_summarizer_agent so it can summarize each video.
4. When video_summarizer_agent comes back, immediately transfer to
   relevance_check_agent so it can check if the videos actually match what the
   user wanted.
5. When relevance_check_agent comes back, check the state:
   - If relevance_retry_needed is true, some videos didn't make the cut. Transfer
     back to search_agent to find replacements (it'll automatically skip videos
     we already tried). Then repeat steps 3-5.
   - If relevance_retry_needed is false (or not set), we're good! Present the
     relevant_videos from state to the user — video titles, channels, durations,
     links, what each video covers, and what the comments say. Keep it friendly
     and easy to read.

Keep the conversation natural. If something goes wrong (like no results found),
let the user know and offer to try a different search. If we ended up with fewer
videos than expected after retries, that's okay — just show what we found.
""",
    sub_agents=[
        query_eval_agent,
        search_agent,
        video_summarizer_agent,
        relevance_check_agent,
    ],
)

# What `adk web` and the chat page run: the agents plus logging + metrics
# for every message, agent, model call and tool call (see Metrics.md).
app = App(name="ytstep", root_agent=root_agent, plugins=[MetricsPlugin()])