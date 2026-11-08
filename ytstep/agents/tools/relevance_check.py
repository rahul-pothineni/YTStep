from google.adk.agents import Context


def get_video_details_for_review(ctx: Context) -> dict:
    """Gather all video details and user requirements for relevance review.

    Pulls together the refined query, search results, and summaries/sentiments
    so the agent can evaluate each video.
    """
    refined_query = ctx.state.get("refined_query", {})
    if not refined_query:
        return {"error": "No refined query found in state."}

    videos = ctx.state.get("search_results", [])
    if not videos:
        return {"error": "No search results found in state."}

    cache = ctx.state.get("video_cache", {})
    pending_summaries = ctx.state.get("pending_summaries", {})
    pending_sentiments = ctx.state.get("pending_sentiments", {})

    video_details = []
    for v in videos:
        vid = v["video_id"]
        if vid in cache:
            summary = cache[vid].get("summary", "")
            sentiment = cache[vid].get("sentiment", "")
        else:
            summary = pending_summaries.get(vid, "No summary available.")
            sentiment = pending_sentiments.get(vid, "No sentiment available.")

        video_details.append({
            "video_id": vid,
            "title": v.get("title", ""),
            "channel": v.get("channel", ""),
            "duration": v.get("duration", ""),
            "duration_seconds": v.get("duration_seconds", 0),
            "summary": summary,
            "sentiment": sentiment,
        })

    return {
        "user_requirements": refined_query,
        "videos": video_details,
    }


def record_relevance_results(
    ctx: Context,
    relevant_video_ids: list[str],
    rejected_videos: list[dict],
) -> dict:
    """Record which videos passed or failed the relevance check.

    Only relevant videos are written to the session cache. Rejected video IDs
    are tracked so the search agent can exclude them on retry.

    Args:
        relevant_video_ids: List of video_id strings that passed.
        rejected_videos: List of {"video_id": str, "reason": str} dicts.
    """
    videos = ctx.state.get("search_results", [])
    cache = ctx.state.get("video_cache", {})
    pending_summaries = ctx.state.get("pending_summaries", {})
    pending_sentiments = ctx.state.get("pending_sentiments", {})
    tried_ids = list(ctx.state.get("tried_video_ids", []))
    retry_count = ctx.state.get("relevance_retry_count", 0)

    # Build lookup of search results by video_id
    video_lookup = {v["video_id"]: v for v in videos}

    # Build relevant_videos list and cache only relevant ones
    relevant_videos = list(ctx.state.get("relevant_videos", []))
    relevant_set = {v["video_id"] for v in relevant_videos}

    for vid in relevant_video_ids:
        if vid in relevant_set:
            continue  # Already approved in a previous iteration
        v = video_lookup.get(vid, {})
        summary = cache.get(vid, {}).get("summary", "") or pending_summaries.get(vid, "")
        sentiment = cache.get(vid, {}).get("sentiment", "") or pending_sentiments.get(vid, "")

        entry = {
            "video_id": vid,
            "title": v.get("title", ""),
            "url": v.get("url", ""),
            "channel": v.get("channel", ""),
            "duration": v.get("duration", ""),
            "duration_seconds": v.get("duration_seconds", 0),
            "summary": summary,
            "sentiment": sentiment,
        }
        relevant_videos.append(entry)

        # Cache only relevant videos
        cache[vid] = {"video_id": vid, "summary": summary, "sentiment": sentiment}

    # Track rejected video IDs
    rejected_ids = []
    for r in rejected_videos:
        rid = r.get("video_id", "")
        if rid and rid not in tried_ids:
            tried_ids.append(rid)
            rejected_ids.append(rid)

    # Determine if retry is needed
    has_rejections = len(rejected_ids) > 0
    retry_count += 1 if has_rejections else 0
    retry_needed = has_rejections and retry_count < 3

    # Write state
    ctx.state["relevant_videos"] = relevant_videos
    ctx.state["video_cache"] = cache
    ctx.state["tried_video_ids"] = tried_ids
    ctx.state["relevance_retry_count"] = retry_count
    ctx.state["relevance_retry_needed"] = retry_needed
    ctx.state["pending_summaries"] = {}
    ctx.state["pending_sentiments"] = {}

    if relevant_videos:
        print(f"[relevance_check] {len(relevant_videos)} relevant videos cached")
    if rejected_ids:
        print(f"[relevance_check] {len(rejected_ids)} videos rejected (retry {'needed' if retry_needed else 'not needed'}, attempt {retry_count}/3)")

    return {
        "relevant_count": len(relevant_videos),
        "rejected_count": len(rejected_ids),
        "retry_needed": retry_needed,
        "retry_count": retry_count,
    }
