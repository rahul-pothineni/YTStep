from google.adk.agents import Context

from ...observability import get_logger, log

_log = get_logger("tools.query")


def record_clarification(ctx: Context, user_answer: str) -> dict:
    """Record a clarification answer from the user.

    Call this after each clarifying question is answered.
    Returns the current count and whether the cap (3) has been reached.
    """
    count = ctx.state.get("clarification_count", 0) + 1
    ctx.state["clarification_count"] = count
    cap = 3
    log(_log, "clarifying answer recorded", count=count, cap_reached=count >= cap)
    return {"count": count, "cap": cap, "cap_reached": count >= cap}


def finalize_query(
    ctx: Context,
    search_topic: str,
    video_type: str = "",
    desired_length: str = "",
    additional_constraints: str = "",
) -> dict:
    """Store the refined query parameters in session state.

    Call this when enough information has been gathered (or cap is reached)
    to proceed with the YouTube search.
    """
    refined = {
        "search_topic": search_topic,
        "video_type": video_type,
        "desired_length": desired_length,
        "additional_constraints": additional_constraints,
    }
    ctx.state["refined_query"] = refined
    log(_log, "request finalized", clarifications=ctx.state.get("clarification_count", 0), **refined)
    return {"status": "finalized", "refined_query": refined}
