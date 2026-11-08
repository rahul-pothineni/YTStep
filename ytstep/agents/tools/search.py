import os
import re

import httpx
from google.adk.agents import Context

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
