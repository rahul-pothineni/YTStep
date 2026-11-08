from google.adk.agents import Agent

from .tools.video_summarizer import analyze_all_sentiments, summarize_all_videos

video_summarizer_agent = Agent(
    name="video_summarizer_agent",
    model="gemini-3.5-flash-lite",
    instruction="""\
You summarize YouTube videos so we can check if they're a good match.
Talk like a friendly, casual person — no jargon or tech speak.

When you're called:
1. Call BOTH tools at the same time — summarize_all_videos and
   analyze_all_sentiments. They each read search results from state and work
   in parallel.
2. Once both come back, do NOT present the results to the user. Just transfer
   back to the orchestrator right away — another agent will handle checking
   the results.
""",
    tools=[summarize_all_videos, analyze_all_sentiments],
    output_key="video_summaries",
)
