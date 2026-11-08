import os

import httpx
from google import genai
from google.genai import types

from google.adk.agents import Context


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

    Checks the cache first — previously approved videos are served from cache.
    Cache misses are processed in parallel via Gemini. Results are stored in
    pending_summaries for the relevance check agent to use.
    """
    from concurrent.futures import ThreadPoolExecutor

    videos = ctx.state.get("search_results", [])
    if not videos:
        return {"error": "No search results found to summarize."}

    google_api_key = os.environ.get("GOOGLE_API_KEY", "")
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

    Stores results in pending_sentiments for the relevance check agent.
    Cache writes happen only after relevance check approves a video.
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

    for r in fetched:
        results.append({**r, "cached": False})

    # Store sentiments for the relevance check agent to use
    # Cache writes happen ONLY in record_relevance_results (for relevant videos)
    ctx.state["pending_sentiments"] = {r["video_id"]: r["sentiment"] for r in fetched}
    print(f"[analyze_all_sentiments] {len(fetched)} videos analyzed")
    return {"sentiments": results, "cache_hits": len(results) - len(fetched), "cache_misses": len(fetched)}
