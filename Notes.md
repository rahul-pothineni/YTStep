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

Using ADK's eval framework (`AgentEvaluator`) with pytest. Each sub-agent is tested in isolation (`agent.parent_agent = None`). One eval case per instruction rule, binary pass/fail. Each rule validated by removing its instruction from the prompt and confirming that specific eval fails.

**query_eval_agent** (6 rules):
- LLM-judged: `one_question_at_a_time`
- Deterministic: `no_finalize_without_topic`, `finalize_with_enough_info`, `calls_record_clarification`, `cap_reached_forces_finalize`, `confirms_before_finalizing`

**search_agent** (5 rules):
- LLM-judged: `specific_search_query`, `does_not_present_results`
- Deterministic: `calls_search_youtube`, `translates_duration`, `leaves_duration_defaults`

Deterministic checks are preferred over LLM judge — only use LLM for subjective qualities (tone, query specificity). Multi-turn behavioral rules (tool call patterns, confirmation text) must be deterministic because the ADK rubric judge evaluates per-invocation, not per-conversation.

Run: `pytest tests/test_evals.py -v -s` (~5 min, 11 cases). Files: `tests/evals/*.test.json`, `tests/evals/test_config.json`.

Problems encountered —

**1. Full pipeline executing during component tests.** Fix: extract sub-agent via `agent_name`, then `agent.parent_agent = None`.

**2. Rubric judge is per-invocation, not per-conversation.** Multi-turn rules always passed because the judge saw each turn independently. Fix: deterministic checks on full invocation trace.

**3. Search agent not reading from state.** User message "Search for videos based on the refined query" caused the agent to search for the literal string "refined query." Fix: user messages must include the actual topic (e.g. "The user wants yoga videos for beginners. Search YouTube for them.").

**4. does_not_present_results rule validation.** When the search tool errors during violation testing, the agent has nothing to present — so the rule passes even with a "present results" instruction. Fix: modified instruction tells agent to fabricate results if the tool fails.

**5. Eval scoring is slow (unresolved, skipped).** During rule validation, some eval cases took ~200s each even though the agent itself finished in ~2s (search tool ~1s). A direct call to the judge model (`gemini-3.8-flash`) returned in ~2s with no retries, so the delay is somewhere inside ADK's eval/scoring step, not the model or the YouTube tool. Earlier full suite runs were ~25s/case, so this may be intermittent. Workaround: per-case timeouts in the validation script (5 min was not always enough). Next step if it matters: log every HTTP request during a slow case to see what fills the gap.

**6. friendly_tone eval removed.** Every way of removing the tone rule (stiff/formal, or friendly-but-jargon) also changed the agent's behavior: it stopped finalizing when the clarification cap was reached, and the formal version skipped the confirmation question. The unmodified agent passed the cap eval every time, so this was a real side effect, not flakiness. Since the tone rule couldn't be tested in isolation, the eval was dropped. The tone rule is still in the agent's instruction, just untested.

**Rule validation status: all 11 rules verified.** For each rule, the rule was removed from a fresh copy of the agent and all of that agent's evals were run: only that rule's eval failed. The unmodified query agent passed all its evals in 2/2 runs.
- Search agent (5/5): `calls_search_youtube`, `specific_search_query`, `translates_duration`, `leaves_duration_defaults`, `does_not_present_results`
- Query eval agent (6/6): `no_finalize_without_topic`, `finalize_with_enough_info`, `one_question_at_a_time`, `calls_record_clarification`, `cap_reached_forces_finalize`, `confirms_before_finalizing`
- Validation-only "not applicable" rule: when removing a rule makes the agent skip a step entirely (e.g. never calls the search tool, or finalizes on the first message), evals that grade that skipped step count as passing instead of failing.

How I used Claude Code for evals —

Prompt: "implement this plan. You can verify after the evals are done writing, if you take out the rule from the agent system prompt the according evals and that eval ONLY should fail. Once you are able to write concrete evals for each rule and verify yourself, then you are done."

This /goal created a self-verifying loop: write evals → swap each rule out → confirm only that eval fails → diagnose and fix if not. Key lessons:

- **Minimal instruction edits only.** Full rewrites of the agent prompt broke other rules. Fix: `str.replace()` on just the target rule's text.
- **Fresh Agent objects, not modified extracts.** Modifying an agent extracted from the pipeline (`agent.instruction = ...`) silently broke ADK's tool registry. Fix: create a new `Agent(instruction=modified, tools=[...])`.
- **Defensive checks for coupled rules.** Some evals depend on upstream behavior (e.g. duration checks need the tool to be called first). When a non-target check can't run, return PASS instead of a false FAIL.
- **Check on long-running tasks.** A validation script ran 40+ min before we realized the approach was wrong. The fix took 2 min. Rule: if a background task shows no progress after 15 min, analyze whether the approach itself is flawed and pivot.


Problem: YTStep never gave timestamps, and on follow-ups ("where does he talk about differential equations?") the orchestrator said it couldn't watch videos.

Root Cause: Gemini already watches every video in `summarize_all_videos`, but the prompt only asked for "a few sentences", so no timestamps were ever produced. Follow-up questions had no tool, so the orchestrator could only reread the short summary.

Solution: The summary prompt now asks for "Key moments" with exact timestamps, picked for what the person asked for (from `refined_query`), and the orchestrator shows them. For follow-ups, the orchestrator has a `find_moments_in_video(video_url, question)` tool that has Gemini watch the video again and return timestamps for the question. "Just give me timestamps" uses the question "the main sections of the video, in order". The page turns `M:SS` into buttons that jump to that moment (bolded timestamps included), and shows "Watching the video to find those moments…" while the tool runs (~10–15 s).
