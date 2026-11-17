"""
ADK agent evaluations for YTStep.

Run with pytest:
    pytest tests/test_evals.py -v -s

Requires GOOGLE_API_KEY and YOUTUBE_API_KEY in ytstep/.env
"""

import json
import logging
import re
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / "ytstep" / ".env")

logging.basicConfig(level=logging.WARNING)
logging.getLogger("google_adk").setLevel(logging.WARNING)
logging.getLogger("google.adk").setLevel(logging.WARNING)

from google.adk.evaluation.agent_evaluator import AgentEvaluator

AGENT_MODULE = "ytstep.agent"
EVAL_DIR = "tests/evals"

CONFIRM_PATTERNS = re.compile(
    r"anything.{0,20}(add|else|change|more|tweak)"
    r"|does (this|that) (look|sound)"
    r"|sound good"
    r"|look right"
    r"|ready to (search|go)"
    r"|before I (go|search|start|look)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_tool_names(invocation) -> list[str]:
    if not invocation or not invocation.intermediate_data:
        return []
    events = invocation.intermediate_data
    names = []
    if hasattr(events, "invocation_events"):
        for ev in events.invocation_events:
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        names.append(part.function_call.name)
    return names


def _extract_tool_args(invocation, tool_name: str) -> dict | None:
    """Return the args dict for the first call to tool_name, or None."""
    if not invocation or not invocation.intermediate_data:
        return None
    events = invocation.intermediate_data
    if hasattr(events, "invocation_events"):
        for ev in events.invocation_events:
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        if part.function_call.name == tool_name:
                            return dict(part.function_call.args or {})
    return None


def _agent_text(invocation) -> str:
    if invocation.final_response and invocation.final_response.parts:
        return invocation.final_response.parts[0].text or ""
    return ""


async def _run_case(case_id: str, eval_file: str, agent_name: str):
    """Run a single eval case and return its invocations + rubric scores."""
    from google.adk.evaluation.eval_config import EvalConfig, get_eval_metrics_from_config
    from google.adk.evaluation.simulation.user_simulator_provider import UserSimulatorProvider
    from google.adk.evaluation.metric_evaluator_registry import (
        DEFAULT_METRIC_EVALUATOR_REGISTRY,
        register_custom_metrics_from_config,
    )

    config_file = f"{EVAL_DIR}/test_config.json"

    with open(config_file) as f:
        eval_config = EvalConfig.model_validate(json.load(f))

    eval_set = AgentEvaluator._load_eval_set_from_file(eval_file, eval_config, {})
    eval_set.eval_cases = [c for c in eval_set.eval_cases if c.eval_id == case_id]
    assert eval_set.eval_cases, f"No eval case with id '{case_id}'"

    agent, app = await AgentEvaluator._get_agent_for_eval(
        module_name=AGENT_MODULE, agent_name=agent_name
    )
    agent.parent_agent = None

    eval_metrics = get_eval_metrics_from_config(eval_config)
    user_sim = UserSimulatorProvider(
        user_simulator_config=eval_config.user_simulator_config
    )
    registry = register_custom_metrics_from_config(
        eval_config, DEFAULT_METRIC_EVALUATOR_REGISTRY.fork()
    )

    results = await AgentEvaluator._get_eval_results_by_eval_id(
        agent_for_eval=agent, eval_set=eval_set, eval_metrics=eval_metrics,
        num_runs=1, user_simulator_provider=user_sim,
        app_name="ytstep", app=app, metric_evaluator_registry=registry,
    )

    invocations = []
    rubric_scores = {}

    for case_results in results.values():
        for cr in case_results:
            for per_inv in (cr.eval_metric_result_per_invocation or []):
                if per_inv.actual_invocation:
                    invocations.append(per_inv.actual_invocation)
                for emr in (per_inv.eval_metric_results or []):
                    if not emr.details or not emr.details.rubric_scores:
                        continue
                    for rs in emr.details.rubric_scores:
                        rubric_scores[rs.rubric_id] = {
                            "passed": (rs.score or 0.0) >= 0.5,
                            "reason": rs.rationale or "",
                        }

    _print_trace(case_id, invocations)
    return invocations, rubric_scores


QUERY_EVAL_FILE = f"{EVAL_DIR}/query_eval_agent.test.json"
SEARCH_EVAL_FILE = f"{EVAL_DIR}/search_agent.test.json"


def _print_trace(case_id: str, invocations: list):
    print(f"\n{'=' * 60}")
    print(f"  CASE: {case_id}")
    print(f"{'=' * 60}")
    for i, inv in enumerate(invocations):
        user_text = ""
        if inv.user_content and inv.user_content.parts:
            user_text = inv.user_content.parts[0].text or ""
        tools = _extract_tool_names(inv)
        agent = _agent_text(inv)
        print(f"  Turn {i + 1}:")
        print(f"    User:  {user_text[:100]}")
        if tools:
            print(f"    Tools: {' -> '.join(tools)}")
        print(f"    Agent: {agent[:150]}")


def _assert_pass(passed: bool, reason: str, rule: str):
    icon = "PASS" if passed else "FAIL"
    print(f"\n  {icon}  {rule}")
    if not passed:
        print(f"    Reason: {reason}")
    assert passed, f"{rule} failed: {reason}"


# ---------------------------------------------------------------------------
# Tests — one per instruction rule
# ---------------------------------------------------------------------------

async def test_friendly_tone():
    """
    Input: "find me a video"
    Expected:
    - Agent responds in casual, friendly language
    - No jargon, bullet lists, or engineer-speak
    """
    _, rubric_scores = await _run_case("friendly_tone", QUERY_EVAL_FILE, "query_eval_agent")
    result = rubric_scores.get("friendly_tone", {"passed": False, "reason": "No rubric score returned"})
    _assert_pass(result["passed"], result["reason"], "friendly_tone")


async def test_no_finalize_without_topic():
    """
    Input: "find me a video"
    Expected:
    - Agent does NOT call finalize_query
    - Agent asks a clarifying question instead
    """
    invocations, _ = await _run_case("no_finalize_without_topic", QUERY_EVAL_FILE, "query_eval_agent")
    tools = _extract_tool_names(invocations[0])
    passed = "finalize_query" not in tools
    reason = "finalize_query was called on a vague request with no topic" if not passed else ""
    _assert_pass(passed, reason, "no_finalize_without_topic")


async def test_finalize_with_enough_info():
    """
    Input: "Find me a 10 minute beginner yoga video for morning stretching"
    Expected:
    - Agent calls finalize_query on the first turn
    - Agent does NOT ask unnecessary follow-up questions
    """
    invocations, _ = await _run_case("finalize_with_enough_info", QUERY_EVAL_FILE, "query_eval_agent")
    tools = _extract_tool_names(invocations[0])
    passed = "finalize_query" in tools
    reason = "Agent did not finalize despite having topic + extras" if not passed else ""
    _assert_pass(passed, reason, "finalize_with_enough_info")


async def test_one_question_at_a_time():
    """
    Input: "find me a video"
    Expected:
    - Agent asks exactly one clarifying question
    - Agent does NOT bundle multiple questions together
    """
    _, rubric_scores = await _run_case("one_question_at_a_time", QUERY_EVAL_FILE, "query_eval_agent")
    result = rubric_scores.get("one_question_at_a_time", {"passed": False, "reason": "No rubric score returned"})
    _assert_pass(result["passed"], result["reason"], "one_question_at_a_time")


async def test_calls_record_clarification():
    """
    Input: "I want to watch something about cooking" → "Italian pasta, carbonara recipe"
    Expected:
    - Agent calls record_clarification after user's second message
    """
    invocations, _ = await _run_case("calls_record_clarification", QUERY_EVAL_FILE, "query_eval_agent")
    if len(invocations) <= 1:
        _assert_pass(False, "Expected multi-turn but got single turn", "calls_record_clarification")
        return
    tools = _extract_tool_names(invocations[1])
    passed = "record_clarification" in tools
    reason = "record_clarification not called after user answered clarifying question" if not passed else ""
    _assert_pass(passed, reason, "calls_record_clarification")


async def test_cap_reached_forces_finalize():
    """
    Input: "show me something fun" → "I dunno, maybe something entertaining"
           (clarification_count starts at 2, so record_clarification returns cap_reached=true)
    Expected:
    - Agent calls record_clarification on turn 2 (count becomes 3, cap_reached=true)
    - Agent calls finalize_query on turn 2 after seeing cap_reached
    """
    invocations, _ = await _run_case("cap_reached_forces_finalize", QUERY_EVAL_FILE, "query_eval_agent")
    if len(invocations) <= 1:
        _assert_pass(False, "Expected multi-turn but got single turn", "cap_reached_forces_finalize")
        return
    tools = _extract_tool_names(invocations[-1])
    has_finalize = "finalize_query" in tools
    reason = "Agent did not call finalize_query when cap was reached" if not has_finalize else ""
    _assert_pass(has_finalize, reason, "cap_reached_forces_finalize")


async def test_confirms_before_finalizing():
    """
    Input: "video" → "fitness" → "a 20 minute home workout for beginners"
    Expected:
    - Before calling finalize_query, agent asks user to confirm
    - Agent text contains a confirmation question (e.g. "anything to add?")
    """
    invocations, _ = await _run_case("confirms_before_finalizing", QUERY_EVAL_FILE, "query_eval_agent")
    for inv in invocations:
        tools = _extract_tool_names(inv)
        if "finalize_query" not in tools:
            continue
        text = _agent_text(inv)
        passed = bool(CONFIRM_PATTERNS.search(text))
        reason = f"finalize_query called without confirmation question. Agent said: {text[:120]}" if not passed else ""
        _assert_pass(passed, reason, "confirms_before_finalizing")
        return
    _assert_pass(True, "", "confirms_before_finalizing")


# ---------------------------------------------------------------------------
# Search Agent — one test per instruction rule
# ---------------------------------------------------------------------------

async def test_calls_search_youtube():
    """
    Input: refined_query with search_topic "yoga for beginners"
    Expected:
    - Agent calls search_youtube tool
    """
    invocations, _ = await _run_case("calls_search_youtube", SEARCH_EVAL_FILE, "search_agent")
    all_tools = []
    for inv in invocations:
        all_tools.extend(_extract_tool_names(inv))
    passed = "search_youtube" in all_tools
    reason = "Agent did not call search_youtube" if not passed else ""
    _assert_pass(passed, reason, "calls_search_youtube")


async def test_specific_search_query():
    """
    Input: refined_query with search_topic "carbonara recipe", video_type "step by step tutorial"
    Expected:
    - search_query arg contains specific terms from refined_query (e.g. "carbonara")
    - Not a generic query like "cooking video"
    """
    invocations, rubric_scores = await _run_case("specific_search_query", SEARCH_EVAL_FILE, "search_agent")
    result = rubric_scores.get("specific_search_query", {"passed": False, "reason": "No rubric score returned"})
    _assert_pass(result["passed"], result["reason"], "specific_search_query")


async def test_translates_duration():
    """
    Input: refined_query with desired_length "around 20 minutes"
    Expected:
    - search_youtube called with min_duration_minutes set (roughly 10-15)
    - search_youtube called with max_duration_minutes set (roughly 25-30)
    """
    invocations, _ = await _run_case("translates_duration", SEARCH_EVAL_FILE, "search_agent")
    args = None
    for inv in invocations:
        args = _extract_tool_args(inv, "search_youtube")
        if args is not None:
            break
    if args is None:
        _assert_pass(False, "search_youtube was never called", "translates_duration")
        return
    min_dur = args.get("min_duration_minutes", 0)
    max_dur = args.get("max_duration_minutes", 120)
    passed = min_dur > 0 and max_dur < 120
    reason = f"Duration not translated: min={min_dur}, max={max_dur}" if not passed else ""
    _assert_pass(passed, reason, "translates_duration")


async def test_leaves_duration_defaults():
    """
    Input: refined_query with desired_length "" (empty)
    Expected:
    - search_youtube called with default duration params (min=0, max=120) or omitted
    """
    invocations, _ = await _run_case("leaves_duration_defaults", SEARCH_EVAL_FILE, "search_agent")
    args = None
    for inv in invocations:
        args = _extract_tool_args(inv, "search_youtube")
        if args is not None:
            break
    if args is None:
        _assert_pass(False, "search_youtube was never called", "leaves_duration_defaults")
        return
    min_dur = args.get("min_duration_minutes", 0)
    max_dur = args.get("max_duration_minutes", 120)
    passed = min_dur == 0 and max_dur >= 120
    reason = f"Duration was set when it shouldn't be: min={min_dur}, max={max_dur}" if not passed else ""
    _assert_pass(passed, reason, "leaves_duration_defaults")


async def test_does_not_present_results():
    """
    Input: refined_query with search_topic "yoga for beginners"
    Expected:
    - Agent does NOT list, describe, or present video results to the user
    - Agent just transfers back to orchestrator
    """
    invocations, rubric_scores = await _run_case("does_not_present_results", SEARCH_EVAL_FILE, "search_agent")
    result = rubric_scores.get("does_not_present_results", {"passed": False, "reason": "No rubric score returned"})
    _assert_pass(result["passed"], result["reason"], "does_not_present_results")
