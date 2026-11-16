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
from google.adk.evaluation.eval_set import EvalSet


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

RULE_THRESHOLD = 0.6


def _fmt_score(score: float | None) -> str:
    if score is None:
        return "-"
    return f"{score:.2f}"


def _extract_tool_names_from_events(invocation) -> list[str]:
    """Extract tool call names from InvocationEvents in an actual invocation."""
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


async def _run_eval(eval_file: str, config_file: str, agent_name: str | None = None):
    """Run eval against the real agent module. Pass agent_name to target a sub-agent."""
    from google.adk.evaluation.eval_config import EvalConfig, get_eval_metrics_from_config
    from google.adk.evaluation.simulation.user_simulator_provider import UserSimulatorProvider
    from google.adk.evaluation.metric_evaluator_registry import (
        DEFAULT_METRIC_EVALUATOR_REGISTRY,
        register_custom_metrics_from_config,
    )

    eval_config = AgentEvaluator.find_config_for_test_file(eval_file)
    if eval_config is None:
        with open(config_file) as f:
            eval_config = EvalConfig.model_validate(json.load(f))

    eval_set = AgentEvaluator._load_eval_set_from_file(eval_file, eval_config, {})

    agent_for_eval, app = await AgentEvaluator._get_agent_for_eval(
        module_name=AGENT_MODULE, agent_name=agent_name
    )
    if agent_name and agent_for_eval.parent_agent:
        agent_for_eval.parent_agent = None
    eval_metrics = get_eval_metrics_from_config(eval_config)
    user_sim = UserSimulatorProvider(
        user_simulator_config=eval_config.user_simulator_config
    )
    metric_evaluator_registry = register_custom_metrics_from_config(
        eval_config, DEFAULT_METRIC_EVALUATOR_REGISTRY.fork()
    )

    eval_results_by_eval_id = await AgentEvaluator._get_eval_results_by_eval_id(
        agent_for_eval=agent_for_eval,
        eval_set=eval_set,
        eval_metrics=eval_metrics,
        num_runs=1,
        user_simulator_provider=user_sim,
        app_name="ytstep",
        app=app,
        metric_evaluator_registry=metric_evaluator_registry,
    )

    return eval_set, eval_results_by_eval_id


def _get_invocations(case_results: list) -> list:
    """Collect all actual invocations across a case's results."""
    invocations = []
    for cr in case_results:
        for per_inv in (cr.eval_metric_result_per_invocation or []):
            if per_inv.actual_invocation:
                invocations.append(per_inv.actual_invocation)
    return invocations


def _check_calls_record_clarification(invocations: list) -> tuple[float, str]:
    """Rule 5: after each clarification answer, record_clarification must be called.

    Deterministic check — inspects actual tool calls across all invocations.
    Returns (score 0.0 or 1.0, reason).
    """
    if len(invocations) <= 1:
        return 1.0, "Single-turn — no clarification to record"

    missed = 0
    checked = 0
    for inv in invocations[1:]:
        tools = _extract_tool_names_from_events(inv)
        has_finalize = "finalize_query" in tools
        has_record = "record_clarification" in tools
        if has_finalize or has_record:
            checked += 1
            if not has_record:
                missed += 1

    if checked == 0:
        return 1.0, "No tool calls after first turn"
    if missed > 0:
        return 0.0, f"record_clarification missing in {missed}/{checked} turn(s)"
    return 1.0, f"record_clarification called in all {checked} turn(s)"


def _check_asks_before_finalizing(invocations: list) -> tuple[float, str]:
    """Rule 6: in multi-turn conversations, confirm with user before finalizing.

    Deterministic check — looks for confirmation text in the agent's response
    on the turn where finalize_query is called.
    Returns (score 0.0 or 1.0, reason).
    """
    if len(invocations) <= 1:
        return 1.0, "Single-turn — confirmation not required"

    for inv in invocations:
        tools = _extract_tool_names_from_events(inv)
        if "finalize_query" not in tools:
            continue
        agent_text = ""
        if inv.final_response and inv.final_response.parts:
            agent_text = inv.final_response.parts[0].text or ""
        if CONFIRM_PATTERNS.search(agent_text):
            return 1.0, "Confirmation found before finalize"
        return 0.0, f"finalize_query called without confirmation question"

    return 1.0, "finalize_query not called"


def _print_results(eval_set: EvalSet, eval_results_by_eval_id: dict) -> bool:
    """Print formatted results with all 6 rule scores per case."""
    print("\n" + "=" * 70)
    print(f"  EVAL SET: {eval_set.name or eval_set.eval_set_id}")
    print("=" * 70)

    case_summaries = []

    for eval_id, case_results in eval_results_by_eval_id.items():
        print(f"\n{'=' * 70}")
        print(f"  CASE: {eval_id}")
        print(f"{'=' * 70}")

        invocations = _get_invocations(case_results)

        # --- Trace ---
        print(f"\n  What happened:")
        for i, inv in enumerate(invocations):
            user_text = ""
            if inv.user_content and inv.user_content.parts:
                user_text = inv.user_content.parts[0].text or ""
            agent_text = ""
            if inv.final_response and inv.final_response.parts:
                agent_text = inv.final_response.parts[0].text or ""
            tools = _extract_tool_names_from_events(inv)

            print(f"    Turn {i + 1}:")
            print(f"      User:  {user_text[:100]}")
            if tools:
                print(f"      Tools: {' -> '.join(tools)}")
            print(f"      Agent: {agent_text[:150]}")

        # --- Collect all 6 rule scores ---
        rule_scores: dict[str, tuple[float, str]] = {}

        # Rules 1-5: from LLM rubric judge
        for cr in case_results:
            for per_inv in (cr.eval_metric_result_per_invocation or []):
                for emr in (per_inv.eval_metric_results or []):
                    if not emr.details or not emr.details.rubric_scores:
                        continue
                    for rs in emr.details.rubric_scores:
                        rule_scores[rs.rubric_id] = (
                            rs.score if rs.score is not None else 0.0,
                            (rs.rationale or "")[:120],
                        )

        # Rules 5-6: deterministic checks on tool calls
        rc_score, rc_reason = _check_calls_record_clarification(invocations)
        rule_scores["calls_record_clarification"] = (rc_score, rc_reason)

        abf_score, abf_reason = _check_asks_before_finalizing(invocations)
        rule_scores["asks_before_finalizing"] = (abf_score, abf_reason)

        # --- Display ---
        print(f"\n  Rule scores:")
        total_score = 0.0
        num_rules = 0
        for rule_id, (score, reason) in rule_scores.items():
            icon = "PASS" if score >= 0.5 else "FAIL"
            print(f"    {icon}  {rule_id}: {_fmt_score(score)}")
            if score < 0.5:
                print(f"          ^ {reason}")
            total_score += score
            num_rules += 1

        normalized = total_score / num_rules if num_rules > 0 else 0.0
        case_passed = normalized >= RULE_THRESHOLD

        print(f"\n  Normalized score: {_fmt_score(normalized)}"
              f" (threshold: {_fmt_score(RULE_THRESHOLD)})")
        verdict = "PASS" if case_passed else "FAIL"
        print(f"  Verdict: {verdict}")
        case_summaries.append((eval_id, verdict, rule_scores, normalized))

    # --- Final summary ---
    passed_count = sum(1 for _, s, _, _ in case_summaries if s == "PASS")
    failed_count = sum(1 for _, s, _, _ in case_summaries if s == "FAIL")
    total = len(case_summaries)

    print(f"\n{'=' * 70}")
    print(f"  SUMMARY: {passed_count}/{total} passed, {failed_count}/{total} failed")
    print(f"{'=' * 70}")
    for eval_id, verdict, scores, norm in case_summaries:
        failing = [rid for rid, (s, _) in scores.items() if s < 0.5]
        print(f"    {verdict}  {eval_id}  ({_fmt_score(norm)})")
        if failing:
            print(f"          Failed: {', '.join(failing)}")
    print("=" * 70 + "\n")

    return failed_count == 0


async def test_query_eval_agent():
    """query_eval_agent in isolation — clarification and finalization behavior."""
    eval_set, results = await _run_eval(
        f"{EVAL_DIR}/query_eval_agent.test.json",
        f"{EVAL_DIR}/test_config.json",
        agent_name="query_eval_agent",
    )
    all_passed = _print_results(eval_set, results)
    assert all_passed, "Some eval cases failed — see output above."
