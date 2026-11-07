import os

import httpx

from google.adk.agents import Context


def record_clarification(ctx: Context, user_answer: str) -> dict:
    """Record a clarification answer from the user.

    Call this after each clarifying question is answered.
    Returns the current count and whether the cap (3) has been reached.
    """
    count = ctx.state.get("clarification_count", 0) + 1
    ctx.state["clarification_count"] = count
    cap = 3
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
    return {"status": "finalized", "refined_query": refined}


MIN_SUBSCRIBER_COUNT = 10_000


def _get_channel_subscribers(api_key: str, channel_ids: list[str]) -> dict[str, int]:
    """Fetch subscriber counts for a batch of channel IDs."""
    if not channel_ids:
        return {}
    resp = httpx.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={
            "part": "statistics",
            "id": ",".join(channel_ids),
            "key": api_key,
        },
        timeout=15,
    )
    resp.raise_for_status()
    counts = {}
    for item in resp.json().get("items", []):
        stats = item.get("statistics", {})
        if stats.get("hiddenSubscriberCount"):
            counts[item["id"]] = 0
        else:
            counts[item["id"]] = int(stats.get("subscriberCount", 0))
    return counts


def search_youtube(ctx: Context, search_query: str, max_results: int = 5) -> dict:
    """Search YouTube for videos matching the query.

    Uses the YouTube Data API v3 to find videos. Only returns videos from
    channels with at least 10k subscribers. Returns title, URL, channel,
    subscriber count, description, and publish date.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key or api_key == "YOUR_KEY_HERE":
        return {"error": "YOUTUBE_API_KEY is not set in .env"}

    # Request more results than needed since some will be filtered out
    fetch_count = min(max_results * 3, 25)

    params = {
        "part": "snippet",
        "q": search_query,
        "type": "video",
        "maxResults": fetch_count,
        "key": api_key,
    }

    try:
        resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"YouTube API error: {e.response.status_code} - {e.response.text}"}
    except httpx.RequestError as e:
        return {"error": f"Request failed: {e}"}

    # Collect videos and their channel IDs
    raw_videos = []
    channel_ids = set()
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        video_id = item.get("id", {}).get("videoId", "")
        channel_id = snippet.get("channelId", "")
        channel_ids.add(channel_id)
        raw_videos.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "channel": snippet.get("channelTitle", ""),
            "channel_id": channel_id,
            "description": snippet.get("description", ""),
            "published_at": snippet.get("publishedAt", ""),
        })

    # Fetch subscriber counts and filter
    try:
        sub_counts = _get_channel_subscribers(api_key, list(channel_ids))
    except (httpx.HTTPStatusError, httpx.RequestError):
        sub_counts = {}

    videos = []
    for v in raw_videos:
        subs = sub_counts.get(v["channel_id"], 0)
        if subs >= MIN_SUBSCRIBER_COUNT:
            v["subscriber_count"] = subs
            del v["channel_id"]
            videos.append(v)
        if len(videos) >= max_results:
            break

    ctx.state["search_results"] = videos
    return {"videos": videos, "count": len(videos)}
