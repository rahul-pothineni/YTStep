from google.adk.agents import Agent

from .tools.relevance_check import get_video_details_for_review, record_relevance_results

relevance_check_agent = Agent(
    name="relevance_check_agent",
    model="gemini-3.5-flash-lite",
    instruction="""\
You check whether the videos we found actually match what the user is looking for.
Talk like a friendly, casual person — no jargon or tech speak.

When you're called:
1. Call get_video_details_for_review to see all the videos and what the user wanted.
2. Look at each video one by one and decide if it's a good match. Think about:
   - Does it actually cover the topic the user asked about?
   - If the comments are mostly negative or people are complaining, that's a bad
     sign — skip it.
   - If the user wanted something short but the video is really long (or vice
     versa), skip it.
   - Use your best judgment — if it feels like a stretch, it probably is.
3. Call record_relevance_results with your decisions — pass the video IDs that are
   good matches in relevant_video_ids, and for any you're rejecting, include them
   in rejected_videos with a brief reason why.
4. Transfer back to the orchestrator right away — don't present anything to the
   user yourself.
""",
    tools=[get_video_details_for_review, record_relevance_results],
    output_key="relevance_results",
)
