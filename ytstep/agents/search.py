from google.adk.agents import Agent

from .tools.search import search_youtube

search_agent = Agent(
    name="search_agent",
    model="gemini-3.5-flash-lite",
    instruction="""\
You search YouTube for videos based on what the user is looking for.

When you're called:
1. Look at the refined_query in state — it has the search topic, video type,
   desired length, and any extra constraints the user mentioned.
2. Come up with a good YouTube search query from that info. Be specific — include
   key terms that will help find the right kind of video.
3. Call the search_youtube tool with your query. If the user mentioned a desired
   video length, translate that into min_duration_minutes and max_duration_minutes.
   For example, if they said "around 20 minutes", use min=10 and max=30. If they
   said "short", use max=10. If they didn't mention length, leave the defaults.
   The tool enforces a hard 2-hour max no matter what.
4. Once you get results, do NOT present them to the user. Just transfer back to
   the orchestrator right away — another agent will handle showing the results.
""",
    tools=[search_youtube],
    output_key="search_results",
)
