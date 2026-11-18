Metrics -

How YTStep measures itself: what gets logged, every metric and how it's calculated, where to look, and how to run evals on real production chats.

Three layers, each answering a different question:

| Layer | Answers | Where it lives | Code |
|---|---|---|---|
| **Logs** | "What happened in this one message, step by step?" | Terminal locally, Vercel → Logs on the live site | `ytstep/observability.py` (`get_logger`, `log`, `timed`) |
| **Metrics** | "How often do we fail, and how fast are we, overall and per agent?" | `metrics_runs` + `metrics_spans` tables (Neon on Vercel, `ytstep/.adk/metrics.db` locally) | `MetricsPlugin` in `ytstep/observability.py`, report: `scripts/metrics_report.py` |
| **Traces** | "Exactly what did each agent see, call and say?" Also the source of eval datasets | Langfuse | `ytstep/tracing.py`, evals: `scripts/langfuse_evals.py` |

Every log line, metrics row and trace for one message shares the same **invocation id**. Each `metrics_runs` row also stores the Langfuse **trace id**, so you can go from a slow or failed number straight to its full trace.

---

## Definitions

- **Message (run / invocation):** one thing a person sends and everything YTStep does until it answers. One message can involve many agents: orchestrator → query_eval → search → summarizer → relevance check → (retry) search → …
- **Agent turn:** one stretch where a single agent is in charge, from when it starts until it hands off (`transfer_to_agent`) or finishes. An agent can have several turns in one message (the orchestrator usually has 3–7).
- **Model call:** one request to Gemini made by an agent to decide what to do or say.
- **Tool call:** one function an agent calls (`search_youtube`, `summarize_all_videos`, …).
- **Outside call (api):** one request a tool makes to YouTube or Gemini (`youtube.search`, `gemini.summarize_video`, …).
- **Full search:** a message that reached `relevance_check_agent`, so a real search ran. **Quick reply:** any other message (clarifying questions, greetings). They're reported separately because their timings differ by ~50×.
- **Percentiles:** p50 = median (half were faster); p95 = 95% were faster (the slow tail people notice).

---

## The metrics

### 1. Failure rate (end to end)

| Metric | Calculation | Source |
|---|---|---|
| **Message failure rate** | (error + no_reply + cut_off) ÷ finished messages | `metrics_runs.status` |
| error | ADK raised, or an event carried an `error_code` (model error, rate limit, crash) | `on_run_error_callback`, `on_event_callback` |
| no_reply | the run ended without any agent sending text the person could read | no non-partial text event |
| cut_off | the row is still `running` 6+ minutes after it started: the server was stopped mid-message (Vercel's 5-minute limit, a crash, a redeploy) | `metrics_runs.status = 'running'` |
| **Degraded rate** | answered messages where ≥1 tool returned an error (e.g. the YouTube API failed and the agent apologized instead of showing videos) ÷ answered | `metrics_spans` with `kind='tool'`, `status='error'` |
| **Failure rate seen by people** | page reports with outcome ≠ answered ÷ all page reports. Catches things the server can't see: dropped connections, the browser going offline, the page never getting the stream | `metrics_spans` with `kind='page'` (sent by `frontend/static/app.js` → `POST /api/client-log`) |
| Tool / model / outside-call failure rate | failed ÷ calls, per tool, per agent's model calls, per outside service | `metrics_spans.status` |
| Searches with no videos | full searches where `videos_returned = 0` | `metrics_runs.videos_returned` |

### 2. Response time

| Metric | Calculation |
|---|---|
| **Response time per message** | `before_run` → `after_run`, server side. p50 / p90 / p95 / max, split into full searches and quick replies |
| Time to first reply text | `before_run` → first readable text event. Today ≈ the full time, because the page asks for non-streaming replies |
| **Wait seen by people** | measured in the browser from pressing send to the end of the stream (`performance.now()`), so it includes network and Vercel |
| **Response time per agent turn** | `before_agent` → next agent's `before_agent` (or `after_agent`). Turns don't overlap, so they add up to the message time |
| **Time per agent per message** | all of an agent's turns in one message added up, plus its **share of total time**. This is the "which sub-agent is slow" number |
| Model call time | `before_model` → `after_model`, per agent, with tokens in/out |
| Tool call time | `before_tool` → `after_tool`, per tool |
| Outside call time | wall time of each YouTube / Gemini request, from `timed()` in the tools |

### 3. Pipeline health and cost

| Metric | Calculation |
|---|---|
| Model calls / tool calls per message | counts on `metrics_runs` |
| Tokens in / out per message | sum of Gemini `usage_metadata` (prompt; candidates + thinking) over the message's model calls |
| Relevance retries per search | `relevance_retry_count` from session state when the message finishes |
| Videos returned per search | `len(relevant_videos)` from session state |
| Cache hits / misses | logged by `summarize_all_videos` / `analyze_all_sentiments` (`summaries: cache checked`) |

---

## Where to look

**One message, live:** the logs.
- Locally: the terminal running `python frontend/server.py` or `adk web`. Readable lines like `14:45:11 INFO ytstep.tools.video gemini.summarize_video invocation=fdc1f615 ms=10180.6 video_id=…`
- Live site: Vercel → project → **Logs**. Lines are JSON there, so you can filter on fields, e.g. `invocation`, `level:error`, `event:"message finished"`.
- Settings: `LOG_LEVEL` (default `INFO`; `DEBUG` for more, `WARNING` for less) and `LOG_FORMAT` (`text` / `json`).

**The numbers:** the report.
```bash
python scripts/metrics_report.py                          # chats on this computer
vercel env pull .env.local                                # once: gets the live database address
python scripts/metrics_report.py --prod --env production  # the live site, last 7 days
python scripts/metrics_report.py --prod --days 1          # just today
```
The report is markdown, so it can be pasted straight into notes or a PR.

**One message, in full:** its Langfuse trace (see below). Find the `trace_id` in `metrics_runs`, or the `trace=` field on the `message received` log line.

**Raw SQL:** the tables are plain. Example, slowest full searches this week:
```sql
select started_at, duration_ms/1000 as seconds, relevance_retries, trace_id
from metrics_runs where environment = 'production' and videos_returned is not null
order by duration_ms desc limit 10;
```

### Tables

`metrics_runs`: one row per message
`invocation_id` (key) · `app_name` · `session_id` · `user_id` · `trace_id` · `environment` (production / preview / local) · `started_at` · `finished_at` · `status` (running / ok / no_reply / error) · `error` · `duration_ms` · `first_reply_ms` · `agent_path` (e.g. `orchestrator > query_eval_agent > search_agent > …`) · `model_calls` · `tool_calls` · `input_tokens` · `output_tokens` · `relevance_retries` · `videos_returned`

`metrics_spans`: one row per timed step
`invocation_id` · `kind` (agent / model / tool / api / page) · `name` · `agent` · `started_at` · `duration_ms` · `status` · `error` · `input_tokens` · `output_tokens` · `details` (JSON, e.g. which tools a model call asked for)

A row is written as `running` when a message starts and completed when it ends, so messages that never finish still show up (as cut_off).

---

## Traces and live evals (Langfuse)

**Tracing.** When `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set, every message is sent to Langfuse as a trace: `invocation` → `agent_run [agent]` → `call_llm` / `execute_tool`, with the full prompts, tool arguments, tool results and replies, plus session id, user id, environment (production / preview / local) and release (git commit). It uses the OpenInference ADK instrumentor, started in `ytstep/agent.py` before the agents load. Traces are flushed at the end of every message, because Vercel can pause the app right after it replies.

Why not Vercel's own Traces page: custom spans only reach it through `@vercel/otel`, which is Node-only. For this Python app Vercel shows the request and its outgoing HTTP calls, but not agents or tools.

**Live evals on production traffic** (no code): in Langfuse → *Evaluators*, add an LLM-as-a-judge evaluator that runs on new traces with `environment = production`. Langfuse scores real chats as they happen (e.g. "did the videos match the request?"), and the scores show up next to each trace and as charts over time.

**Production datasets → experiments** (`scripts/langfuse_evals.py`):
```bash
python scripts/langfuse_evals.py collect --days 7          # live-site chats → dataset "ytstep-production-chats"
python scripts/langfuse_evals.py run --name "new search prompt"   # replay them with the agents on this computer, scored
python scripts/langfuse_evals.py try "beginner yoga, 20 min, go search"   # test the scoring, no Langfuse needed
```
- `collect` reads chats from Neon (via `metrics_runs`, `environment = production`). Each chat becomes one dataset item: the person's messages in order, with the live site's reply and video ids in metadata and a link to the production trace. Re-running it skips chats already in the dataset. You can also add single traces by hand in Langfuse's UI ("Add to dataset").
- `run` replays every item through the **current** code (so change a prompt, then run) and records an experiment in Langfuse with these scores per chat:

| Score | Meaning |
|---|---|
| `answered` | the replay produced a reply with no errors |
| `videos_found` | number of videos recommended |
| `seconds` | replay time |
| `relevance_judge` | 0–1 from a Gemini judge: do the videos fit what the person asked? (a sensible clarifying question on a vague request = 1) |
| `same_videos_as_production` | share of production's videos the replay also picked |
| `avg_relevance_judge`, `answer_rate` | whole-run averages |

Compare experiment runs side by side in Langfuse → *Datasets* → `ytstep-production-chats` to see whether a change helped. These replays don't write to the metrics tables (`YTSTEP_METRICS=off`), so they never count as real traffic.

How this relates to the pytest evals (`tests/test_evals.py`): those check one rule per agent with hand-written cases and are pass/fail. The Langfuse experiments check whole chats taken from real use and give scores. Use both: pytest so a rule never breaks, experiments to see whether real chats get better.

**Privacy.** Traces and datasets contain what people typed. Langfuse stores them on its cloud (the region set by `LANGFUSE_BASE_URL`). Tell users their chats may be reviewed to improve YTStep.

---

## Baseline (local, 2026-11-18)

A first sample from testing on this computer: a handful of messages, so it shows the shape of the numbers, not reliable percentiles. Replace this with `--prod` numbers once there's real traffic.

| | Value |
|---|---|
| Full search, end to end | 22–53 s (one search with 2 relevance retries took 52.5 s) |
| Quick reply (clarifying question) | ~1 s |
| Share of a full search's time | video_summarizer ~61%, search ~15%, orchestrator ~12%, relevance ~8%, query_eval ~4% |
| `gemini.summarize_video` | p50 11.1 s, p95 14.0 s per video (the three videos run in parallel) |
| Agent model calls (flash-lite) | ~0.6–1.5 s each, 12–24 per full search |
| Tokens per full search | ~105k in / ~1k out. Input grows with every handoff because each agent re-reads the conversation |
| YouTube API calls | 0.2–0.6 s each |

**What this points at:** video summarization is most of the wait. Each relevance retry repeats search + summarize (~18 s). Input tokens are dominated by the conversation being re-sent on each of the ~20 model calls.
