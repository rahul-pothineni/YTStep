# About:
Currently a work in progress.

Check out the [Notes.md](Notes.md) file for more information about the motivation.

# System Architecture:
<img width="455" height="563" alt="Screenshot 2026-11-08 at 3 50 51 PM" src="https://github.com/user-attachments/assets/8257e45e-793f-4387-8764-f740320749e8" />

## How to Run

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set up environment variables** — create a `.env` file in the project root:
   ```
   GOOGLE_API_KEY=your_gemini_api_key
   YOUTUBE_API_KEY=your_youtube_data_api_key
   ```

3. **Start the ADK web server:**
   ```bash
   adk web
   ```

4. Open the URL shown in the terminal and start chatting with the agent.

## Testing One Agent at a Time

`adk web` also lists each agent on its own, so you can try it without running the whole pipeline. Pick one from the app dropdown:

| App | What it runs | What to type |
|---|---|---|
| `ytstep` | The full pipeline | Any video request |
| `ytstep_query_eval` | Just the question-asking agent | A request, e.g. "find me a video" |
| `ytstep_search` | Just the YouTube search agent | What the user wants, e.g. "The user wants beginner yoga videos, about 20 minutes long. Search YouTube for them." |
| `ytstep_video_summarizer` | Just the summarizer | Anything, e.g. "go" |
| `ytstep_relevance_check` | Just the relevance checker | Anything, e.g. "go" |

The summarizer and relevance checker normally get their videos from the agents before them. In these test apps they start from a `sample_state.json` in their folder, taken from a real earlier run. Edit that file to try different videos. The relevance sample includes one off-topic video on purpose, so the checker has something to reject.

Each test app runs the real agent (same instructions and tools). Changes in `ytstep/agents/` show up here automatically.

**Saving a chat as a test case:** open the **Eval** tab on the right, create an eval set, and click **Add current session**. You can rerun saved cases from that tab later. They're stored as `<app folder>/<name>.evalset.json`.

The automated eval cases (`tests/evals/*.test.json`) also appear in the Eval tab: `ytstep_query_eval` and `ytstep_search` each hold a link to their file, so the UI and `pytest` always use the same cases. The tab scores with its own built-in checks, not the pass/fail rules in `tests/evals/test_config.json`, so use `pytest` for the official result.

## Running Evals

Evals test each agent in isolation against the real agent code. They use Google ADK's eval framework with an LLM-as-a-judge for quality scoring.

1. **Install eval dependencies:**
   ```bash
   pip install 'google-adk[eval]'
   ```

2. **Run the eval suite:**
   ```bash
   pytest tests/test_evals.py -v -s
   ```

The query eval agent tests take ~30 seconds to complete (4 cases with LLM inference + judge scoring). Eval files live in `tests/evals/`.
