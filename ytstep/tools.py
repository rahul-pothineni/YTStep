import os
import re

import httpx
from google import genai
from google.genai import types

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
MAX_DURATION_SECONDS = 2 * 60 * 60  # 2 hours


def _parse_iso8601_duration(duration: str) -> int:
    """Parse ISO 8601 duration (e.g. 'PT1H30M15S') to total seconds."""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _get_video_durations(api_key: str, video_ids: list[str]) -> dict[str, int]:
    """Fetch durations in seconds for a batch of video IDs."""
    if not video_ids:
        return {}
    resp = httpx.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "contentDetails",
            "id": ",".join(video_ids),
            "key": api_key,
        },
        timeout=15,
    )
    resp.raise_for_status()
    durations = {}
    for item in resp.json().get("items", []):
        raw = item.get("contentDetails", {}).get("duration", "")
        durations[item["id"]] = _parse_iso8601_duration(raw)
    return durations


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


def search_youtube(
    ctx: Context,
    search_query: str,
    max_results: int = 3,
    min_duration_minutes: int = 0,
    max_duration_minutes: int = 120,
) -> dict:
    """Search YouTube for videos matching the query.

    Uses the YouTube Data API v3 to find videos. Only returns videos from
    channels with at least 10k subscribers and within the duration range.
    Hard cap of 2 hours max regardless of input.

    Args:
        search_query: The YouTube search string.
        max_results: Number of videos to return (default 3).
        min_duration_minutes: Minimum video length in minutes (default 0).
        max_duration_minutes: Maximum video length in minutes (default/cap 120).
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

    # Fetch subscriber counts and filter by 10k+ subs
    try:
        sub_counts = _get_channel_subscribers(api_key, list(channel_ids))
    except (httpx.HTTPStatusError, httpx.RequestError):
        sub_counts = {}

    sub_filtered = []
    for v in raw_videos:
        subs = sub_counts.get(v["channel_id"], 0)
        if subs >= MIN_SUBSCRIBER_COUNT:
            v["subscriber_count"] = subs
            sub_filtered.append(v)

    # Enforce hard 2-hour cap, then convert to seconds
    min_secs = max(min_duration_minutes, 0) * 60
    max_secs = min(max_duration_minutes, MAX_DURATION_SECONDS // 60) * 60

    # Fetch durations and filter by range
    video_ids = [v["video_id"] for v in sub_filtered]
    try:
        durations = _get_video_durations(api_key, video_ids)
    except (httpx.HTTPStatusError, httpx.RequestError):
        durations = {}

    videos = []
    for v in sub_filtered:
        dur = durations.get(v["video_id"], 0)
        if dur > 0 and min_secs <= dur <= max_secs:
            v["duration_seconds"] = dur
            minutes, secs = divmod(dur, 60)
            hours, minutes = divmod(minutes, 60)
            v["duration"] = f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"
            del v["channel_id"]
            videos.append(v)
        if len(videos) >= max_results:
            break

    ctx.state["search_results"] = videos
    return {"videos": videos, "count": len(videos)}


def _get_video_comments(api_key: str, video_id: str, max_comments: int = 50) -> list[str]:
    """Fetch top comments for a video, sorted by relevance."""
    try:
        resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params={
                "part": "snippet",
                "videoId": video_id,
                "maxResults": max_comments,
                "order": "relevance",
                "textFormat": "plainText",
                "key": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        comments = []
        for item in resp.json().get("items", []):
            text = item["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            comments.append(text)
        return comments
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []


def summarize_all_videos(ctx: Context) -> dict:
    """Summarize all videos from search_results using the Gemini API.

    Processes all videos in parallel. On a cache miss, both summary and
    sentiment are computed together and cached as one entry.
    """
    from concurrent.futures import ThreadPoolExecutor

    videos = ctx.state.get("search_results", [])
    if not videos:
        return {"error": "No search results found to summarize."}

    google_api_key = os.environ.get("GOOGLE_API_KEY", "")
    youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not google_api_key:
        return {"error": "GOOGLE_API_KEY is not set in .env"}

    cache = ctx.state.get("video_cache", {})
    results = []
    to_process = []

    for v in videos:
        vid = v["video_id"]
        if vid in cache:
            results.append({**cache[vid], "cached": True})
        else:
            to_process.append(v)

    if results:
        print(f"[summarize_all_videos] CACHE HIT — {len(results)} videos served from cache")

    if not to_process:
        return {"summaries": results, "cache_hits": len(results), "cache_misses": 0}

    print(f"[summarize_all_videos] CACHE MISS — {len(to_process)} to process")

    client = genai.Client(api_key=google_api_key)

    def _summarize_one(video):
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=[
                    "Summarize this YouTube video. Focus on the main topics covered, "
                    "key points, and what a viewer would learn from it. "
                    "Keep it concise — a few sentences.",
                    types.Part.from_uri(
                        file_uri=video["url"],
                        mime_type="video/mp4",
                    ),
                ],
            )
            return response.text
        except Exception as e:
            return f"Could not summarize: {e}"

    with ThreadPoolExecutor(max_workers=len(to_process)) as pool:
        fetched = list(pool.map(lambda v: {"video_id": v["video_id"], "summary": _summarize_one(v)}, to_process))

    for r in fetched:
        results.append({**r, "cached": False})

    # Don't cache yet — wait for sentiment to complete too
    ctx.state["pending_summaries"] = {r["video_id"]: r["summary"] for r in fetched}
    return {"summaries": results, "cache_hits": len(results) - len(fetched), "cache_misses": len(fetched)}


def analyze_all_sentiments(ctx: Context) -> dict:
    """Fetch top comments and analyze sentiment for all videos in search_results.

    After this completes, combines with pending summaries and writes both
    to the cache as one entry per video.
    """
    from concurrent.futures import ThreadPoolExecutor

    videos = ctx.state.get("search_results", [])
    if not videos:
        return {"error": "No search results found."}

    google_api_key = os.environ.get("GOOGLE_API_KEY", "")
    youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not google_api_key:
        return {"error": "GOOGLE_API_KEY is not set in .env"}

    cache = ctx.state.get("video_cache", {})
    results = []
    to_process = []

    for v in videos:
        vid = v["video_id"]
        if vid in cache:
            results.append({"video_id": vid, "sentiment": cache[vid]["sentiment"], "cached": True})
        else:
            to_process.append(v)

    if results:
        print(f"[analyze_all_sentiments] CACHE HIT — {len(results)} videos served from cache")

    if not to_process:
        return {"sentiments": results, "cache_hits": len(results), "cache_misses": 0}

    print(f"[analyze_all_sentiments] CACHE MISS — {len(to_process)} to process")

    client = genai.Client(api_key=google_api_key)

    def _analyze_one(video):
        comments = _get_video_comments(youtube_api_key, video["video_id"])
        if not comments:
            return "No comments available."
        comments_text = "\n".join(comments[:50])
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[
                    "Here are the top comments from a YouTube video. "
                    "Give a brief sentiment summary: is this video well-received? "
                    "What do viewers like or dislike about it? "
                    "Keep it to 2-3 sentences, casual tone.\n\n"
                    f"Comments:\n{comments_text}",
                ],
            )
            return response.text
        except Exception as e:
            return f"Could not analyze: {e}"

    with ThreadPoolExecutor(max_workers=len(to_process)) as pool:
        fetched = list(pool.map(lambda v: {"video_id": v["video_id"], "sentiment": _analyze_one(v)}, to_process))

    # Combine with pending summaries and write to cache as one entry
    pending = ctx.state.get("pending_summaries", {})
    for r in fetched:
        vid = r["video_id"]
        summary = pending.get(vid, "")
        cache[vid] = {"video_id": vid, "summary": summary, "sentiment": r["sentiment"]}
        results.append({**r, "cached": False})

    ctx.state["video_cache"] = cache
    ctx.state["pending_summaries"] = {}
    print(f"[analyze_all_sentiments] Cached {len(fetched)} new videos (total cached: {len(cache)})")
    return {"sentiments": results, "cache_hits": len(results) - len(fetched), "cache_misses": len(fetched)}
