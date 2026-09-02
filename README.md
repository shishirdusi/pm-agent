# PM Agent — Meeting Notes → Task Tracker

A small agent that reads meeting notes (Gemini/Fireflies-style summaries, transcripts, or manual
notes), extracts tasks with owner/status/priority/blocker, merges them into a running tracker
without creating duplicates, and requires a human to approve every change before it's saved.

```
Meeting Notes → Task Extraction → Owner/Status/Blocker Detection → Human Review → Tracker Update
```

## Setup

```bash
git clone <this repo>
cd pm_agent

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# then open .env and paste in your ANTHROPIC_API_KEY (get one at console.anthropic.com)
```

No API key? It still runs — extraction falls back to a cached demo extraction for the 4 sample
meetings included here, or a basic rule-based extractor for anything else. Nothing about the
review/tracker/dedupe logic depends on having a key.

## Running it

**See the full demo (3 real meetings processed back-to-back, scripted review decisions):**
```bash
python3 demo_run.py
```

**Run it on your own meeting note, with real interactive review in your terminal:**
```bash
python3 run_pipeline.py --notes path/to/notes.txt --meeting-date 2026-08-12
```
You'll be prompted to approve/edit/ignore/split each proposed task right there in the terminal.
If `ANTHROPIC_API_KEY` is set (via `.env` or `export`), extraction uses the real Claude API.

**Push to a real Google Sheet** (optional, on top of the CSV):
```bash
# in .env: GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/key.json, PM_AGENT_SHEET_ID=<sheet id>
python3 run_pipeline.py --notes path/to/notes.txt --meeting-date 2026-08-12 --write-sheet
```

**Run the tests:**
```bash
pytest -v
```
27 tests covering the dedupe/merge logic, the review-decision application (approve/edit/
ignore/split), CSV round-tripping, and the extractor's fallback tiers — no network calls, no
API key required.

**Or use the local web UI** (talks directly to the same backend modules - no logic duplicated
in JavaScript, and your API key never leaves the server):
```bash
python3 server.py
```
Then open **http://127.0.0.1:8000**. Paste in meeting notes, click Extract, review each
proposed task (approve/edit/ignore/split) right in the browser, and the tracker table updates
live from the same `output/task_tracker.csv` the CLI writes to - so you can freely mix using
the terminal and the browser on the same tracker.

## Deploying it

By default `server.py` only runs on your own machine (`http://127.0.0.1:8000`) - you have to
start it before each use, and only you can reach it. To make it available all the time without
running a terminal command first, deploy it to a small always-on host. **Render's free tier**
is the simplest option (no credit card, GitHub-integrated):

1. Push this repo to GitHub (see the git/GitHub steps earlier in this README's history, or
   just `git push` if you've already got a remote set up).
2. Go to [render.com](https://render.com), sign in with GitHub, click **New +** → **Web Service**.
3. Pick this repo. Render should auto-detect the `Procfile` (`web: uvicorn server:app --host
   0.0.0.0 --port $PORT`) - if it asks for a build/start command manually instead, use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
4. Under **Environment**, add these variables (Render's dashboard, not your local `.env` -
   `.env` never gets deployed since it's gitignored, which is correct):
   - `ANTHROPIC_API_KEY` = your key
   - `APP_USERNAME` = a username you pick
   - `APP_PASSWORD` = a password you pick

   **Don't skip the last two.** Without them, anyone who finds your Render URL can use the
   tool and spend your API credits - `server.py` only skips the login when both are unset,
   which is meant for local-only use, not a public deployment.
5. Click **Create Web Service**. Render builds and deploys it, and gives you a URL like
   `https://pm-agent-xyz.onrender.com`. Open it, log in with the username/password from step 4,
   and it's the same tool, just always on.

One real trade-off with Render's free tier: it spins down after periods of inactivity and takes
~30-60 seconds to wake back up on the next request. Fine for occasional personal/team use; if
that delay is annoying, Render's cheapest paid tier (or Railway/Fly.io, which work almost
identically) removes it.

Whichever host you use, the tracker CSV lives on that server's disk, not synced anywhere else -
if you care about not losing it if the service is ever redeployed/recreated, that's the case
for eventually moving from a CSV to a real database (see the "what's next" ideas below) or
wiring up the Google Sheets writer as a second, durable copy.

## Files

| File | What it does |
|---|---|
| `extractor.py` | Meeting text → list of task dicts. Tries the real Claude API first (if `ANTHROPIC_API_KEY` is set), falls back to a cached demo extraction for the sample meetings, falls back to a simple regex extractor as a last resort. |
| `tracker.py` | Loads/saves the tracker CSV. Matches new tasks against existing rows (same workstream + similar title) to propose **add** vs **update**, so the same task doesn't get duplicated across meetings. |
| `review.py` | The human-in-the-loop gate. Approve / edit / ignore / split, for every proposed change. Nothing reaches the tracker without going through here. |
| `run_pipeline.py` | Runs one meeting note through the full flow, interactively (terminal). |
| `server.py` | Local web UI. A thin FastAPI layer over the exact same `extractor.py`/`tracker.py`/`review.py` - no logic reimplemented in JS. Run it, open `http://127.0.0.1:8000`. |
| `static/index.html` | The browser frontend for `server.py`. Pure rendering + `fetch()` calls to `/api/*` - all the actual decision-making happens server-side. |
| `sheets_writer.py` | Optional: pushes the tracker to a real Google Sheet if credentials are configured. CSV always works with no setup. |
| `demo_run.py` | Runs 3 real standups through the pipeline back-to-back with pre-scripted (but realistic) review decisions, so the whole flow — including dedupe, status updates, and a reviewer catching two bad auto-matches — is visible without typing anything. |
| `tests/` | Automated tests (pytest) for the dedupe/merge logic, review decisions, and extractor fallbacks. |
| `sample_meetings/` | 4 real standup notes (condensed Gemini-style summaries) used for the demo and tests. |
| `cache/` | Pre-generated extractions for the sample meetings, so the demo runs with no API key. |
| `output/` | Where `task_tracker.csv` lands after a run. Gitignored by default (see `.gitignore`) since it's your team's real data, not sample data. |

## Tracker columns

`workstream, task, description, owner, status, priority, due_date, blocker, last_updated, follow_up_needed, notes`

## Design notes

- **Dedupe key is (workstream, title-similarity) — not owner.** Ownership sometimes shifts
  between meetings; if it were part of the match key, a reassigned task would silently create a
  second row instead of surfacing the reassignment for review.
- **Title similarity is a heuristic, not a guarantee.** Two real cases in the demo turn out to be
  different people's different work that just read similarly. The reviewer catches both and uses
  "split" to add them as new rows instead. There's also a documented edge case in the tests
  (`test_title_similarity_short_near_identical_titles_can_overmatch`) where very short titles can
  over-match — real task titles are descriptive phrases, so this mostly matters as a reminder that
  the review step is load-bearing, not decorative.
- **Unclear owner → `TBD`, not a guess.** Same for unclear status → `Not Started` with a note
  explaining why, so nothing silently looks more resolved than it is.
- **The full run history lives in `notes`**, not a separate audit table, since this is meant to
  stay a single readable sheet a PM can scan, not a database.
