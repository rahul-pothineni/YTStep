Purpose - 
I was curious to see how when you search for a video on google it gets the exact timestamps where your question was asked. I wanted to implement this a little deeper however. When I am doing research or learning about a topic I find that LLMs struggle to respond with contextualized videos of what I truly am asking for. 

Goal -

An orchestrator agent takes in the user's query and passes it off to a series of sub-agents:

1. **Query Evaluation Agent** — Evaluates the user's query and checks if there's enough information to search YouTube. If not, it comes up with clarifying questions to ask the user. This runs in a loop until the agent is satisfied (with a cap of k retries, after which it forces forward with whatever info it has).

2. **Search Agent** — Takes the refined information from the query evaluation and searches YouTube for potential videos using the YouTube Data API. Only returns videos from channels with 10k+ subscribers and within the requested duration range. On retries, it excludes previously rejected video IDs.

3. **Video Analyzer** — Looks through each video using the Gemini API (with session caching for re-queries) and fetches the top 50 comments for sentiment analysis. Both tasks run in parallel per video. Extracts summaries, relevant timestamps, and audience reception. Only videos that pass the relevance check get cached.

4. **Relevance Check Agent** — Checks the results from step 3 against the constraints from step 1 — is the video actually on topic? Are the comments mostly negative? Is it way too long for what the user wanted? Videos that don't pass get rejected with a reason, and the system loops back to the Search Agent to find replacements (with a cap of k retries). Videos that pass get returned to the orchestrator with their summaries, timestamps, and sentiment.

At the end, with all information accumulated (video links, timestamps, summaries, and comment sentiment), the orchestrator presents the results to the user. Each sub-agent provides its results and summary of work to the next agent and the orchestrator.

Problem: Video summarization via Gemini API takes too long. 

**Sol 1** (top 3 videos): Fewer parallel Gemini calls = faster and reduces cost. Trade-off is a smaller candidate pool, but YouTube's ranking already puts the best results first.                                                                     
                  
Sol 2 (transcript-based): Grab captions via youtube-transcript-api and summarize text instead of video. Orders of magnitude faster. Fall back to video processing only for videos without captions. Will not work, need a 3rd party library against TOS. 

**Sol 3**: top 50 comments → get sentiment and any useful information. Make agents ignore attempts at humor. Increases accuracy, increases cost.

Problem: The orchestrator skips video_summarizer_agent after search_agent completes.                                                                    
                  
Root Cause: Agent routing is LLM-driven and the orchestrator decides which agent to transfer to next. The search_agent instruction told it to present results to the user, so by the time control returned to the orchestrator, the LLM saw results were already shared and decided the job was done, skipping the summarizer. 

Solution: Removed the "present results" step from search_agent. It now silently hands back to the orchestrator after calling search_youtube, forcing the orchestrator to continue the chain to video_summarizer_agent since no results have been shown yet.



