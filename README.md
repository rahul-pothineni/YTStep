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
