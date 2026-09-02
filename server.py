"""
server.py
A local web UI for the PM Agent that talks directly to the real backend modules -
extractor.py, tracker.py, review.py - the exact same code run_pipeline.py and
demo_run.py use. No logic is reimplemented in JavaScript here; the browser just
renders whatever these functions return and posts back your review decisions.

Run locally: python3 server.py
Then open: http://127.0.0.1:8000

Deployed (Render/Railway/Fly/etc): the platform sets $PORT and calls
`uvicorn server:app --host 0.0.0.0 --port $PORT` for you - see the Procfile and
the "Deploying it" section in README.md.

Your ANTHROPIC_API_KEY (from .env locally, or a platform env var when deployed)
is used server-side only, in extractor.py's normal live-API code path - it never
goes anywhere near the browser.

Basic auth: once this is reachable from the internet instead of just your own
machine, anyone with the URL could use it and burn through your API key's quota.
If APP_USERNAME and APP_PASSWORD are both set (as env vars), every request
requires that login. If neither is set (the default for local use), no auth is
applied - matching the original local-only behavior exactly. Strongly recommended
to set both before deploying anywhere public.
"""

import base64
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from extractor import extract_tasks
from tracker import load_tracker, save_tracker, propose_merge
from review import ReviewDecision, apply_decisions

BASE_DIR = Path(__file__).parent
TRACKER_PATH = BASE_DIR / "output" / "task_tracker.csv"
STATIC_DIR = BASE_DIR / "static"

APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
AUTH_ENABLED = bool(APP_USERNAME and APP_PASSWORD)

app = FastAPI(title="PM Agent")


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """
    Covers EVERYTHING - the API routes below and the static HTML/JS/CSS files -
    in one place, so the browser's native login prompt reliably appears on the
    very first page load, not just on individual API calls.

    No-op when APP_USERNAME/APP_PASSWORD aren't set (local dev - unchanged
    behavior from before this was added). Set both before deploying anywhere
    public, or anyone with the URL can use your API key's quota.
    """
    if not AUTH_ENABLED:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    valid = False
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
            valid = (
                secrets.compare_digest(username, APP_USERNAME)
                and secrets.compare_digest(password, APP_PASSWORD)
            )
        except Exception:
            valid = False

    if not valid:
        return Response(
            content="Authentication required.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="PM Agent"'},
        )
    return await call_next(request)


class ExtractRequest(BaseModel):
    notes: str
    meeting_date: str


class DecisionIn(BaseModel):
    action: str  # "approve" | "edit" | "ignore" | "split"
    edits: Optional[dict] = None


class ApplyRequest(BaseModel):
    proposals: list[dict]
    decisions: list[DecisionIn]


@app.get("/api/tracker")
def get_tracker():
    """Current tracker state, straight from the CSV on disk - the real source of truth."""
    return {"rows": load_tracker(TRACKER_PATH)}


@app.post("/api/proposals")
def get_proposals(req: ExtractRequest):
    """
    Runs the real extractor.extract_tasks() (live Claude API if ANTHROPIC_API_KEY is
    set, otherwise the same rule-based fallback the CLI uses - meeting_path=None
    means it deliberately never touches the cached-demo tier, since these are your
    real notes, not one of the bundled sample meetings) and the real
    tracker.propose_merge() against whatever's currently in the tracker CSV.
    """
    if not req.notes.strip():
        raise HTTPException(400, "Meeting notes are empty.")
    if not req.meeting_date.strip():
        raise HTTPException(400, "Meeting date is required.")

    tasks, method = extract_tasks(req.notes, meeting_path=None)
    if not tasks:
        return {"proposals": [], "method": method}

    existing = load_tracker(TRACKER_PATH)
    proposals = propose_merge(existing, tasks, req.meeting_date)
    return {"proposals": proposals, "method": method}


@app.post("/api/apply")
def apply(req: ApplyRequest):
    """
    Runs the real review.apply_decisions() and tracker.save_tracker(). Reloads the
    tracker from disk right before applying (rather than trusting a client-held
    copy) so this stays correct even if you've got the page open in two tabs.
    """
    if len(req.decisions) != len(req.proposals):
        raise HTTPException(400, "Number of decisions must match number of proposals.")

    existing = load_tracker(TRACKER_PATH)
    decisions = [ReviewDecision(i, d.action, d.edits) for i, d in enumerate(req.decisions)]
    new_rows = apply_decisions(req.proposals, decisions, existing)
    save_tracker(TRACKER_PATH, new_rows)
    return {"rows": new_rows}


@app.post("/api/reset")
def reset_tracker():
    """
    Clears every row from the tracker. Genuinely destructive and not undoable from
    here - the frontend is expected to confirm with the person before calling this
    (see static/index.html). Writes an empty tracker (header row only) rather than
    deleting the file, so load_tracker() keeps working the same way either way.
    """
    save_tracker(TRACKER_PATH, [])
    return {"rows": []}


# Serve the frontend last, so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # 0.0.0.0 is required for cloud platforms to route traffic in; it's also fine
    # for local use (127.0.0.1 was only ever a minor extra restriction, not a
    # feature anything here depended on).
    host = "0.0.0.0"
    url = f"http://127.0.0.1:{port}" if port == 8000 else f"http://0.0.0.0:{port}"
    print(f"\nPM Agent running at {url}  (Ctrl+C to stop)")
    if AUTH_ENABLED:
        print("Basic auth is ON (APP_USERNAME/APP_PASSWORD are set).\n")
    else:
        print("Basic auth is OFF - anyone who can reach this URL can use it. "
              "Set APP_USERNAME and APP_PASSWORD before deploying anywhere public.\n")
    uvicorn.run(app, host=host, port=port)
