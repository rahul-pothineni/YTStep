# YTStep - Multi-Agent YouTube Video Search System

## Project Overview
A multi-agent system that takes a user's query, refines it through clarifying questions, searches YouTube for relevant videos, analyzes video content via Gemini API, and returns timestamped summaries of the best matching videos.

**Language:** Python 3.14+

---
**Frameworks:** Google ADK, Gemini API

---
**NOTES:** To start we will be interfacing with the Agents throguh the Google ADK web server. This is done through the command adk web. 

This project is for non-developers/students. Do not have the agents user "engineer-speak" when responding and asking questions!

---
## Architecture

```
User
  │ Queries
  ▼
Frontend Interface
  │                        ▲ Success (Summary + Timestamps)
  │                        │ Failure (k retries exceeded, rate limited, unable to fetch)
  ▼                        │
┌──────────────────────────────────────────────────────────────────────────────┐
│  HARNESS                                                                     │
│                                                                              │
│  ┌──────────────┐  pass user query   ┌────────────────────────────────────┐  │
│  │ Orchestrator  │──────────────────▶│  1. Query Evaluation Agent          │  │
│  │  (central     │◀─────────────────│  Evaluates user query + previous    │  │
│  │   coordinator)│  not enough info  │  given info.                        │  │
│  │               │  (clarifying Qs)  │  Decides: enough info or not.       │  │
│  │               │  loop w/ k retries│  (loop cap k → force continue)      │  │
│  │               │                   └──────────┬─────────────────────────┘  │
│  │               │                              │ enough info                │
│  │               │                              ▼                            │
│  │               │   ┌──────────────────────────────────────────────────┐    │
│  │               │   │  2. Search Agent  (uses YT API Tool)             │    │
│  │               │   │  Takes refined info, creates YT search queries,  │    │
│  │               │   │  searches YouTube for potential videos.           │    │
│  │               │   └──────────┬───────────────────────────────────────┘    │
│  │               │              │ pass potential video links + required info  │
│  │               │              ▼                                             │
│  │               │   ┌─────────────────┐                                     │
│  │               │   │  Session Cache   │◀── First checks cache              │
│  │               │   │                  │──▶ Cache hit: reuse both            │
│  │               │   │                  │    Cache miss: process below        │
│  │               │   └──┬──────────────┘                                     │
│  │               │      │ cache miss (both run in parallel)                   │
│  │               │      ├───────────────────────┬────────────────────────┐    │
│  │               │      ▼                       ▼                        │    │
│  │               │   ┌────────────────────┐  ┌──────────────────────┐   │    │
│  │               │   │ 3a. Gemini API     │  │ 3b. Comment Analysis │   │    │
│  │               │   │ Summarizes video   │  │ Fetches first 50     │   │    │
│  │               │   │ content + extracts │  │ comments, analyzes   │   │    │
│  │               │   │ relevant timestamps│  │ sentiment & useful   │   │    │
│  │               │   │ (in parallel)      │  │ info (in parallel)   │   │    │
│  │               │   └────────┬───────────┘  └──────────┬───────────┘   │    │
│  │               │            └──────────┬──────────────┘               │    │
│  │               │                       ▼                              │    │
│  │               │            ┌─────────────────┐                       │    │
│  │               │            │  Session Cache   │◀── Write combined    │    │
│  │               │            │  (summary +      │    result to cache   │    │
│  │               │            │   sentiment per  │    (one entry per    │    │
│  │               │            │   video ID)      │     video ID)        │    │
│  │               │            └────────┬────────┘                       │    │
│  │               │                     │ summary + timestamps + sentiment│    │
│  │               │   ┌─────────────────▼────────────────────────────────┘    │
│  │               │   │  4. Relevance Check Agent                             │
│  │               │   │  Checks if video is relevant to user reqs.           │
│  │               │   │                                                       │
│  │               │   │  - If RELEVANT ──▶ return Summary + Timestamps        │
│  │               │   │    to Orchestrator                                    │
│  │               │   │  - If NOT RELEVANT ──▶ state reason why +             │
│  │               │   │    send tried video ID back to Search Agent            │
│  │               │   │    (loop with k retries → then fail)                  │
│  │               │   └───────────────────────────────────────────────────┘    │
│  │               │                                                           │
│  │  ◀────────────┤  Summary + Timestamps (if relevant)                       │
│  └──────────────┘                                                            │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Agent Descriptions

### 1. Orchestrator
- **Role:** Central coordinator for the entire pipeline
- **Inputs:** User query from Frontend Interface
- **Outputs:** Final results (video links + timestamps + summaries) or failure message
- **Behavior:**
  - Receives initial user query
  - Passes query to Query Evaluation Agent
  - Relays clarifying questions back to user via Frontend
  - Receives accumulated info from all sub-agents
  - Returns final results to Frontend on success

### 2. Query Evaluation Agent
- **Role:** Evaluate whether enough information exists to perform a useful search
- **Inputs:** User's original query + any previously provided info
- **Outputs:** Either "enough info" (proceed to Search) or clarifying questions (loop back)
- **Loop behavior:**
  - If not enough info → generates clarifying questions → sends back to Orchestrator → user answers → re-evaluated
  - Loop cap: **k retries**, then force continue with whatever info is available
- **Info sent forward:** Refined query constraints, search parameters, relevance criteria

### 3. Search Agent
- **Role:** Search YouTube for candidate videos
- **Inputs:** Refined query info + constraints from Query Evaluation Agent + list of already-tried video IDs (from Relevance Check rejections)
- **Outputs:** List of potential video links + metadata
- **Behavior:**
  - Constructs YouTube search queries from gathered info
  - Searches YouTube and returns candidate video list
  - Excludes previously rejected video IDs on retry loops

### 4. Video Analyzer (Gemini API + Comment Sentiment)
- **Role:** Analyze video content and audience reception
- **Inputs:** Video links from Search Agent
- **Outputs:** Content summary + relevant timestamps + comment sentiment for each video
- **Parallel processing:** Two tasks run simultaneously per video:
  - **3a. Gemini API** — Summarizes video content and extracts relevant timestamps
  - **3b. Comment Analysis** — Fetches first 50 comments (by relevance), analyzes sentiment and useful info
- **Caching:**
  - **Session Cache** — checks cache first (keyed by video ID)
  - Cache hit → reuse existing summary + sentiment (skip both tasks)
  - Cache miss → run both tasks in parallel → write combined result (summary + sentiment) to cache as one entry per video ID
  - One cache entry = one video. If cache miss, both tasks must run — no partial cache states.
- **Tech:** Google Gemini API for video understanding, YouTube Data API for comments

### 5. Relevance Check Agent
- **Role:** Validate that analyzed videos actually match user requirements
- **Inputs:** Video summaries + timestamps from Video Analyzer + original constraints from Query Evaluation Agent
- **Outputs:** Either "relevant" (pass results up) or "not relevant" (loop back to Search Agent with reason + tried video ID)
- **Loop behavior:**
  - If not relevant → sends reason + tried video ID back to Search Agent for new candidates
  - Loop cap: **k retries**, then fail
- **On success:** Passes final summary + timestamps to Orchestrator

---

## Data Flow Summary

1. **User → Frontend → Orchestrator → Query Eval Agent** (clarification loop, max k)
2. **Query Eval Agent → Search Agent** (once enough info gathered, uses YT API tool)
3. **Search Agent → Session Cache check** (cache hit → skip to step 5, cache miss → step 4)
4. **Cache miss → parallel: Gemini API (summarize + timestamps) + Comment Analysis (sentiment)** → write combined result to cache
5. **Summary + Timestamps + Sentiment → Relevance Check Agent** (relevance loop, max k)
6. **Relevance Check → Orchestrator → Frontend → User** (on success)
7. **Relevance Check (not relevant) → Search Agent** (retry with tried video ID, loop with k retries → then fail)

### Failure Modes
- Query Evaluation exceeds k retries → force continue with available info
- Relevance Check exceeds k retries → fail with error to user
- Gemini API rate limited / unable to fetch → fail with error to user

### Inter-Agent Communication
- Each sub-agent provides its **results + summary of work** to both the next agent in the pipeline and back to the Orchestrator
- Orchestrator sends query context forward for use by downstream agents (e.g., relevance check)
- Orchestrator maintains full context of the pipeline state
