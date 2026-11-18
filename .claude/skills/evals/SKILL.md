---
name: evals
description: How to write, add, modify, and verify evals for the YTStep agents (Google ADK eval framework + LLM judge, tests/test_evals.py and tests/evals/*.test.json). Use when writing or changing evals for any agent in this project.
---
# Eval Skill

## When to use
When writing, modifying, or adding evals for any agent in this project.

## Eval principles

1. **One rule per eval.** Each test function tests exactly one rule from the agent's system prompt. Name the test after the rule.

2. **Binary pass/fail only.** No 0–1 scales, no 1–5 scores, no normalized averages. Every check returns PASS or FAIL.

3. **Every rule in the agent's instruction gets an eval.** Read the agent's `instruction` string, extract each behavioral rule, and write one eval case + one test function per rule.

4. **Expected behavior as bullets.** Each eval case documents what the agent should do as a short bulleted list. Put this in a docstring on the test function:
   ```python
   async def test_no_finalize_without_topic():
       """
       Input: "find me a video"
       Expected:
       - Agent does NOT call finalize_query
       - Agent asks a clarifying question
       """
   ```

5. **LLM judge prompts are concise.** One sentence max. Ask a yes/no question. Example:
   - Good: `"Did the agent ask only one question (not multiple bundled together)?"`
   - Bad: `"When asking for clarification, the agent asks only ONE question at a time, not multiple questions bundled together. The agent should not bombard the user with several questions in a single response."`

6. **LLM judge must give a reason.** The judge's rationale is printed on failure so you know exactly why it failed.

7. **Prefer deterministic checks over LLM judge.** If you can verify a rule by inspecting tool calls or matching text patterns, do that instead of asking an LLM. Use LLM judge only for subjective qualities (tone, naturalness).

8. **Deterministic checks:**
   - Tool was/wasn't called → inspect `_extract_tool_names(invocation)`
   - Tool arguments → `_extract_tool_args(invocation, "<tool_name>")` (returns `None` if not called)
   - Text pattern present → regex on `inv.final_response.parts[0].text`
   - Tool call count → count occurrences in tool names list

9. **Eval case structure in .test.json:**
   - Each case has one `eval_id` matching the rule name
   - Single-turn cases: one conversation entry
   - Multi-turn cases: multiple conversation entries with user simulator responses
   - `session_input.state` can pre-set state (e.g. `clarification_count: 2` to test cap behavior)

10. **Invoke the real production agent, do not create a seperate dummy to simulate it**

## File layout

```
tests/
  test_evals.py              # All eval test functions
  evals/
    <agent_name>.test.json   # Eval cases for one agent
    test_config.json          # Shared rubric config (LLM judge settings)
```

## Adding evals for a new agent

1. Read the agent's `instruction` string
2. List every behavioral rule as a bullet
3. For each rule, decide: deterministic check or LLM judge?
4. Write one eval case in `<agent_name>.test.json` per rule
5. Write one `test_<rule_name>` function in `test_evals.py` per rule
6. Each test function has a docstring with Input + Expected bullets
7. Run: `pytest tests/test_evals.py::test_<rule_name> -v -s`

## Running evals

```bash
# All evals
pytest tests/test_evals.py -v -s

# Single rule
pytest tests/test_evals.py::test_one_question_at_a_time -v -s
```

## Verifying evals

Each eval must prove it catches its rule's violation. For every rule:

1. **Minimal edit only.** Start from the real instruction, `str.replace()` only the targeted rule's text. Never rewrite the full instruction — that breaks other rules.

2. **Create a fresh Agent.** Don't modify an extracted agent (`agent.instruction = ...` breaks ADK tool registration). Instead:
   ```python
   agent = Agent(
       name="agent_name",
       model="gemini-3.5-flash-lite",
       instruction=modified_instruction,
       tools=[tool_fn],  # same tools as the real agent
   )
   ```

3. **Run ALL cases for that agent, not just the target.** The target eval must FAIL and every other eval must PASS.

4. **Defensive checks for coupled rules.** Some checks depend on earlier behavior (e.g. duration checks need the tool to be called). When a non-target check can't run because the flow was disrupted, return PASS (not applicable) instead of a false FAIL:
   ```python
   if args is None:
       return True, "search_youtube not called — duration not testable"
   ```

5. **Check in on long tasks.** If a validation run takes 15+ min with no progress, check if the approach is wrong (broken tool registry, wrong API, etc.) and pivot rather than wait.

6. **Violations that work:**
   - Tone rules → don't give them a standalone eval: a tone-only violation can't be isolated, because changing tone also changes behavior (skipped confirmations, not finalizing at the cap). This is the one exception to "every rule gets an eval".
   - Tool-calling rules → remove the tool from `tools=[]`
   - Parameter rules → "NEVER set X" or "ALWAYS set X=5"
   - Presentation rules → "present results to the user, fabricate if needed"
