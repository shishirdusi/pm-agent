"""
server.py
A local web UI for the PM Agent that talks directly to the real backend modules -
extractor.py, tracker.py, review.py - the exact same code run_pipeline.py and
demo_run.py use. No logic is reimplemented in JavaScript here; the browser just
renders whatever these functions return and posts back your review decisions.

Run: python3 server.py
Then open: http://127.0.0.1:8000

Your ANTHROPIC_API_KEY (from .env) is used server-side only, in extractor.py's
normal live-API code path - it never goes anywhere near the browser, so there's
no key-in-a-static-file problem like the standalone HTML prototype had.
"""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from extractor import extract_tasks
from tracker import load_tracker, save_tracker, propose_merge
from review import ReviewDecision, apply_decisions

BASE_DIR = Path(__file__).parent
TRACKER_PATH = BASE_DIR / "output" / "task_tracker.csv"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="PM Agent")


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


# Serve the frontend last, so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    print("\nPM Agent running at http://127.0.0.1:8000  (Ctrl+C to stop)\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)
