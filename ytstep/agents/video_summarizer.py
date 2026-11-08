from google.adk.agents import Agent

from .tools.video_summarizer import analyze_all_sentiments, summarize_all_videos

video_summarizer_agent = Agent(
    name="video_summarizer_agent",
    model="gemini-3.5-flash-lite",
    instruction="""\
You summarize YouTube videos so the user can pick the best one.
Talk like a friendly, casual person — no jargon or tech speak.

When you're called:
1. Call BOTH tools at the same time — summarize_all_videos and
   analyze_all_sentiments. They each read search results from state and work
   in parallel.
2. Once both come back, present the results to the user in a friendly way.
   For each video, share:
   - The title, channel, and duration
   - A summary of what the video covers
   - What the comments say — is it well-received? Any common praise or complaints?
3. Transfer back to the orchestrator when you're done.
""",
    tools=[summarize_all_videos, analyze_all_sentiments],
    output_key="video_summaries",
)
