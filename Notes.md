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

**Sol 4** (agentic video processing): Switch from Gemini's static mode (1 FPS, ~6,000 tokens per 60s video) to agentic mode. The model dynamically navigates the video timeline, loading only the transcript/frames it needs based on the prompt. Up to 88% fewer tokens and ~7% higher quality on long-form content. Supported on gemini-3.5-flash-lite/3.5-flash and newer. Uses the interactions API (`client.interactions.create`) with `"processing": "agentic"` on the video input.

Problem: The orchestrator skips video_summarizer_agent after search_agent completes.                                                                    
                  
Root Cause: Agent routing is LLM-driven and the orchestrator decides which agent to transfer to next. The search_agent instruction told it to present results to the user, so by the time control returned to the orchestrator, the LLM saw results were already shared and decided the job was done, skipping the summarizer. 

Solution: Removed the "present results" step from search_agent. It now silently hands back to the orchestrator after calling search_youtube, forcing the orchestrator to continue the chain to video_summarizer_agent since no results have been shown yet.

Evaluation Strategy —

Using ADK's eval framework (`AgentEvaluator`) with pytest. Evals run against the real agents — no mocks. Each sub-agent is tested in isolation by extracting it via `agent_name` and detaching its parent (`agent.parent_agent = None`) so the pipeline stops at the agent boundary.

Each eval case is scored on **7 rules** (each 0.0–1.0), normalized to 0.0–1.0 with a 0.6 pass threshold. Each rule was validated by removing its instruction from the agent prompt and confirming that specific rule scores 0.0.

**LLM-judged** (gemini-3.8-flash per-invocation rubric):
1. `friendly_casual_tone` — no jargon or engineer-speak
2. `no_finalize_without_topic` — don't finalize on vague requests
3. `finalize_when_enough_info` — don't over-ask when topic + extras given
4. `one_question_at_a_time` — single question per turn
5. `correct_tool_sequencing` — tools called in sensible order

**Deterministic** (inspect actual tool calls across full trace):
6. `calls_record_clarification` — called after each clarification answer in multi-turn
7. `asks_before_finalizing` — confirmation text present before `finalize_query` in multi-turn

Rules 6–7 are deterministic because the ADK rubric judge evaluates per-invocation (one turn), so it can't assess multi-turn patterns — it always saw "single turn, rule N/A → 1.0."

Run: `pytest tests/test_evals.py -v -s` (~60s, 4 cases). Files: `tests/evals/*.test.json`, `tests/evals/test_config.json`.

Problems encountered —

**1. Full pipeline executing during component tests.** Targeting the root orchestrator ran every sub-agent. Fix: extract sub-agent via `agent_name`, then `agent.parent_agent = None`. Test time dropped from minutes to ~10 seconds.

**2. Criteria too strict for LLM non-determinism.** `IN_ORDER` trajectory at 1.0 failed when the agent asked 3 questions instead of 2. Fix: trajectory and response match at 0.0 (informational only), rubric normalized score at 0.6 as the real gate.

**3. Rubric judge is per-invocation, not per-conversation.** Multi-turn rules (record_clarification, confirm before finalize) always passed because the judge saw each turn independently. Fix: replaced those rubrics with deterministic checks that inspect the full invocation trace.

