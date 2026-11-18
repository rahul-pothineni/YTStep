import logging
import os
import re

import httpx
from google import genai

from google.adk.agents import Context

from ...observability import get_logger, log, map_in_threads, timed

_log = get_logger("tools.video")


def _get_video_comments(api_key: str, video_id: str, max_comments: int = 50) -> list[str]:
    """Fetch top comments for a video, sorted by relevance."""
    try:
        with timed(_log, "youtube.comments", video_id=video_id) as info:
            resp = httpx.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={
                    "part": "snippet",
                    "videoId": video_id,
                    "maxResults": max_comments,
                    "order": "relevance",
                    "textFormat": "plainText",
                },
                timeout=15,
                headers={"X-goog-api-key": api_key},  # header, not URL: keeps the key out of logs
            )
            info["http_status"] = resp.status_code
            resp.raise_for_status()
            comments = []
            for item in resp.json().get("items", []):
                text = item["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
                comments.append(text)
            info["comments"] = len(comments)
        return comments
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []  # already logged by timed(); comments are optional


VIDEO_MODEL = "gemini-3.5-flash"
_YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})")


def _watch(client, video_url: str, prompt: str) -> str:
    """Has Gemini watch a YouTube video and answer `prompt` about it."""
    interaction = client.interactions.create(
        model=VIDEO_MODEL,
        input=[
            {"type": "video", "uri": video_url, "processing": "agentic"},
            {"type": "text", "text": prompt},
        ],
    )
    return interaction.output_text


def _what_they_want(ctx: Context) -> str:
    refined = ctx.state.get("refined_query") or {}
    wishes = [refined.get("search_topic"), refined.get("video_type"), refined.get("additional_constraints")]
    return ", ".join(w for w in wishes if w) or "learning about this topic"


SUMMARY_PROMPT = """\
Summarize this YouTube video for someone who wants: {wants}.

Write:
1. A few sentences on the main topics and what a viewer would learn.
2. "Key moments:" followed by 3 to 6 bullet points, each starting with the exact
   timestamp where that part begins (M:SS, or H:MM:SS past an hour), then a short
   description. Pick the moments most useful for what this person wants.
   Example: "- 4:12 Why regular calculus breaks down with randomness"

Only use timestamps you actually saw in the video. Keep it plain and friendly."""

MOMENTS_PROMPT = """\
Watch this YouTube video and answer this question about it: {question}

Give the exact timestamps (M:SS, or H:MM:SS past an hour) where the relevant parts
start, as bullet points like "- 12:40 Where he starts solving the equation", with a
one-line description each. If the video never covers it, say so plainly and name
the closest part with its timestamp. Only use timestamps you actually saw."""


def find_moments_in_video(ctx: Context, video_url: str, question: str) -> dict:
    """Watches a YouTube video and finds the timestamps for what the person asks about.

    Use this for follow-up questions about a video, like "where does he talk
    about differential equations?" or "give me timestamps for the first video".

    Args:
        video_url: The YouTube link of the video (from the videos shown earlier).
        question: What the person wants to find in the video, in their words.
    """
    match = _YOUTUBE_ID.search(video_url)
    if not match:
        return {"error": f"That doesn't look like a YouTube link: {video_url}"}
    video_id = match.group(1)
    google_api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not google_api_key:
        return {"error": "GOOGLE_API_KEY is not set in .env"}

    client = genai.Client(api_key=google_api_key)
    try:
        with timed(_log, "gemini.find_moments", video_id=video_id, model=VIDEO_MODEL) as info:
            answer = _watch(client, f"https://www.youtube.com/watch?v={video_id}",
                            MOMENTS_PROMPT.format(question=question))
            info["chars"] = len(answer or "")
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not watch the video: {e}"}
    return {"video_id": video_id, "video_url": f"https://www.youtube.com/watch?v={video_id}", "moments": answer}


def summarize_all_videos(ctx: Context) -> dict:
    """Summarize all videos from search_results using the Gemini API.

    Checks the cache first — previously approved videos are served from cache.
    Cache misses are processed in parallel via Gemini. Results are stored in
    pending_summaries for the relevance check agent to use.
    """
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

    log(_log, "summaries: cache checked", cache_hits=len(results), cache_misses=len(to_process))

    if not to_process:
        return {"summaries": results, "cache_hits": len(results), "cache_misses": 0}

    client = genai.Client(api_key=google_api_key)
    prompt = SUMMARY_PROMPT.format(wants=_what_they_want(ctx))

    def _summarize_one(video):
        try:
            with timed(_log, "gemini.summarize_video", video_id=video["video_id"], model=VIDEO_MODEL) as info:
                summary = _watch(client, video["url"], prompt)
                info["chars"] = len(summary or "")
                return summary
        except Exception as e:
            return f"Could not summarize: {e}"

    fetched = map_in_threads(lambda v: {"video_id": v["video_id"], "summary": _summarize_one(v)}, to_process)

    succeeded = [r for r in fetched if not r["summary"].startswith("Could not summarize:")]
    failed = [r for r in fetched if r["summary"].startswith("Could not summarize:")]

    if failed:
        log(_log, "some summaries failed", logging.WARNING, failed=len(failed), succeeded=len(succeeded),
            videos=[r["video_id"] for r in failed])

    for r in succeeded:
        results.append({**r, "cached": False})

    if not results:
        log(_log, "all summaries failed", logging.ERROR, failed=len(failed))
        return {"error": "All videos failed to summarize.", "failed_count": len(failed)}

    # Don't cache yet — wait for sentiment to complete too
    ctx.state["pending_summaries"] = {r["video_id"]: r["summary"] for r in succeeded}
    # Update search_results to only include videos that succeeded + cached hits
    successful_ids = {r["video_id"] for r in succeeded} | {vid for vid in cache if any(v["video_id"] == vid for v in videos)}
    ctx.state["search_results"] = [v for v in videos if v["video_id"] in successful_ids]
    return {"summaries": results, "cache_hits": len(results) - len(succeeded), "cache_misses": len(succeeded), "failed_count": len(failed)}


def analyze_all_sentiments(ctx: Context) -> dict:
    """Fetch top comments and analyze sentiment for all videos in search_results.

    Stores results in pending_sentiments for the relevance check agent.
    Cache writes happen only after relevance check approves a video.
    """
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

    log(_log, "sentiments: cache checked", cache_hits=len(results), cache_misses=len(to_process))

    if not to_process:
        return {"sentiments": results, "cache_hits": len(results), "cache_misses": 0}

    client = genai.Client(api_key=google_api_key)

    def _analyze_one(video):
        comments = _get_video_comments(youtube_api_key, video["video_id"])
        if not comments:
            return "No comments available."
        comments_text = "\n".join(comments[:50])
        try:
            with timed(_log, "gemini.comment_sentiment", video_id=video["video_id"], model="gemini-3.5-flash-lite",
                       comments=len(comments)):
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

    fetched = map_in_threads(lambda v: {"video_id": v["video_id"], "sentiment": _analyze_one(v)}, to_process)

    succeeded = [r for r in fetched if not r["sentiment"].startswith("Could not analyze:")]
    failed = [r for r in fetched if r["sentiment"].startswith("Could not analyze:")]

    if failed:
        log(_log, "some sentiment checks failed", logging.WARNING, failed=len(failed),
            succeeded=len(succeeded), videos=[r["video_id"] for r in failed])

    for r in succeeded:
        results.append({**r, "cached": False})

    if not results:
        log(_log, "all sentiment checks failed", logging.ERROR, failed=len(failed))
        return {"error": "All sentiment analyses failed.", "failed_count": len(failed)}

    # Store sentiments for the relevance check agent to use
    # Cache writes happen ONLY in record_relevance_results (for relevant videos)
    ctx.state["pending_sentiments"] = {r["video_id"]: r["sentiment"] for r in succeeded}
    # Update search_results to only include videos that succeeded + cached hits
    successful_ids = {r["video_id"] for r in succeeded} | {vid for vid in cache if any(v["video_id"] == vid for v in videos)}
    ctx.state["search_results"] = [v for v in ctx.state.get("search_results", []) if v["video_id"] in successful_ids]
    return {"sentiments": results, "cache_hits": len(results) - len(succeeded), "cache_misses": len(succeeded), "failed_count": len(failed)}
