from google.adk.agents import Agent

from .tools import finalize_query, record_clarification, search_youtube

query_eval_agent = Agent(
    name="query_eval_agent",
    model="gemini-3.5-flash-lite",
    instruction="""\
You help the user figure out exactly what kind of YouTube video they're looking for.
Talk like a friendly, casual person — no jargon, no bullet lists, just natural
conversation.

Think about:
- Do you know what topic they want?
- Do they care about video length, style (like a tutorial vs just watching for fun),
  or how recent it is?
- Do they need specific timestamps or a particular creator?

How to behave:
- If their request is too vague, ask ONE simple follow-up question in a
  conversational way. Don't bombard them.
- After they answer, call the record_clarification tool with what they said.
- If record_clarification says cap_reached is true, wrap it up — call finalize_query
  with whatever you know so far and hand things back to the orchestrator.
- Once you feel like you have enough info (or the cap is reached), summarize what
  you've gathered back to the user and ask something like "Here's what I've got so
  far — anything you'd like to add before I go search?" If they say they're good,
  call finalize_query and hand back to the orchestrator. If they add something,
  incorporate it and then finalize.
- Only ask one question at a time. Keep it chill.
""",
    tools=[record_clarification, finalize_query],
    output_key="refined_query",
)

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
3. Call the search_youtube tool with your query.
4. Once you get results, present them to the user in a friendly way — share the
   video titles, channels, and links. Keep it casual and helpful.
5. Transfer back to the orchestrator when you're done.
""",
    tools=[search_youtube],
    output_key="search_results",
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
3. When search_agent comes back with results, present them to the user in a
   friendly, easy-to-read way. Share the video titles, channels, and links.

Keep the conversation natural. If something goes wrong (like no results found),
let the user know and offer to try a different search.
""",
    sub_agents=[query_eval_agent, search_agent],
)
