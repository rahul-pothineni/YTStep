Purpose - 
I was curious to see how when you search for a video on google it gets the exact timestamps where your question was asked. I wanted to implement this a little deeper however. When I am doing research or learning about a topic I find that LLMs struggle to respond with contextualized videos of what I truly am asking for. 

Goal - 

I will create a multi-agent loop that will ask the user a multitude of questions before going off and searching.

Have an orchestrator agent take in the users query → pass it off to 2… At the end with all information accumulated (video links + timestamps and summaries)
Have an agent evaluate the user's query and check if the provided information is relevant enough to step into 3 or not. If not comes up with questions to ask (this runs in a loop until this agent is satisfied with some cap k) 
Another agent that takes the information gathered and searches youtube for potential videos and lists potential videos. 
An agent that looks through the video (gemini api + local caching for requeries), length, and specific timestamps (if requested) of these videos and chooses the best ones for the users requests.
Check the results of 4 vs. the constraints from 2 and relevance. Run in a loop with cap k or we pass the constraints and relevance. 

At the end of each subagents task it will provide their results and summary of their work to the next agent and the orchestrator. 

Problem: Video summarization via Gemini API takes too long. 

**Sol 1** (top 3 videos): Fewer parallel Gemini calls = faster and reduces cost. Trade-off is a smaller candidate pool, but YouTube's ranking already puts the best results first.                                                                     
                  
Sol 2 (transcript-based): Grab captions via youtube-transcript-api and summarize text instead of video. Orders of magnitude faster. Fall back to video processing only for videos without captions. Will not work, need a 3rd party library against TOS. 

**Sol 3**: top 50 comments → get sentiment and any useful information. Make agents ignore attempts at humor. Increases accuracy, increases cost.

Problem: The orchestrator skips video_summarizer_agent after search_agent completes.                                                                    
                  
Root Cause: Agent routing is LLM-driven and the orchestrator decides which agent to transfer to next. The search_agent instruction told it to present results to the user, so by the time control returned to the orchestrator, the LLM saw results were already shared and decided the job was done, skipping the summarizer. 

Solution: Removed the "present results" step from search_agent. It now silently hands back to the orchestrator after calling search_youtube, forcing the orchestrator to continue the chain to video_summarizer_agent since no results have been shown yet.



