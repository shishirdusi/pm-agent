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
import hashlib
import hmac
import os
import secrets
import time
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from extractor import extract_tasks
from tracker import load_tracker, save_tracker, propose_merge, COLUMNS
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

    Exempts /slack/events - Slack authenticates that endpoint with its own
    request-signature scheme (see _verify_slack_signature below), not a
    username/password, since Slack's servers can't be given login credentials.
    """
    if not AUTH_ENABLED or request.url.path.startswith("/slack/"):
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


@app.get("/api/tracker/csv")
def download_csv():
    """
    Streams the real tracker CSV straight off disk with proper download headers -
    this is the exact same file save_tracker() writes, not a JS-rebuilt copy, so
    there's no risk of it drifting from what the CLI/tests produce.
    """
    if not TRACKER_PATH.exists():
        raise HTTPException(404, "No tracker file yet - add at least one task first.")
    csv_bytes = TRACKER_PATH.read_bytes()
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="task_tracker.csv"'},
    )


@app.put("/api/tracker/{row_index}")
def edit_tracker_row(row_index: int, edits: dict):
    """
    Directly edits an existing tracker row - for fixing a mistake in a task that's
    already been approved, without waiting for it to come up again in a future
    meeting. Unlike /api/apply, this bypasses the propose/review flow entirely
    since there's no new meeting note driving it - it's a direct correction.
    """
    rows = load_tracker(TRACKER_PATH)
    if row_index < 0 or row_index >= len(rows):
        raise HTTPException(404, f"No tracker row at index {row_index} (tracker has {len(rows)} rows).")
    unknown_fields = set(edits.keys()) - set(COLUMNS)
    if unknown_fields:
        raise HTTPException(400, f"Unknown field(s): {', '.join(sorted(unknown_fields))}")
    rows[row_index].update(edits)
    save_tracker(TRACKER_PATH, rows)
    return {"rows": rows}


@app.delete("/api/tracker/{row_index}")
def delete_tracker_row(row_index: int):
    """Removes a single row - for a task that was approved by mistake or is no longer relevant."""
    rows = load_tracker(TRACKER_PATH)
    if row_index < 0 or row_index >= len(rows):
        raise HTTPException(404, f"No tracker row at index {row_index} (tracker has {len(rows)} rows).")
    rows.pop(row_index)
    save_tracker(TRACKER_PATH, rows)
    return {"rows": rows}


# ---------------------------------------------------------------------------
# Slack integration
#
# Lets meeting notes get fed in by posting them in a Slack channel, instead of
# copy-pasting into the web UI. This is genuinely optional - nothing else in
# this file depends on it - and it never bypasses human review: it only DRAFTS
# proposed tasks and posts them back into the Slack thread, pointing at the web
# UI to actually approve/edit/ignore/split. That review step is the one thing
# this project is built around, and a message arriving via Slack instead of a
# text box isn't a reason to skip it.
#
# Setup (see README.md "Slack bot" section for the full walkthrough):
#   1. Create a Slack app at api.slack.com/apps, add the chat:write bot scope,
#      install it to your workspace, and invite it to the channel you'll post
#      notes in.
#   2. Under "Event Subscriptions", turn events on, set the Request URL to
#      https://<your-deployed-url>/slack/events (this REQUIRES a public URL -
#      Slack can't reach http://127.0.0.1, so this step needs your Render
#      deployment, or a tool like ngrok for local testing), and subscribe to
#      the "message.channels" bot event.
#   3. Set SLACK_BOT_TOKEN (starts with xoxb-) and SLACK_SIGNING_SECRET (both
#      from the app's Slack dashboard) as environment variables. Optionally
#      set SLACK_NOTES_CHANNEL_ID to restrict this to one channel - without
#      it, ANY channel the bot is invited to will trigger extraction on every
#      message, which is probably not what you want.
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_NOTES_CHANNEL_ID = os.environ.get("SLACK_NOTES_CHANNEL_ID")  # optional
SLACK_ENABLED = bool(SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET)


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """
    Confirms a request genuinely came from Slack using Slack's documented HMAC
    signing scheme - not just "did someone POST to this URL". Also rejects
    requests with a timestamp more than 5 minutes old, to block replay attacks
    (someone capturing and re-sending a previously-valid request).
    """
    if not SLACK_SIGNING_SECRET:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except (ValueError, TypeError):
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"), basestring.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature or "")


def _process_slack_message(channel: str, thread_ts: str, text: str) -> None:
    """
    Runs as a background task, AFTER Slack's webhook already got its 200 OK
    (Slack requires a fast ack and retries if it doesn't get one - doing the
    actual extraction here instead of inline keeps the initial response fast
    regardless of how long extraction takes). Posts a plain-text summary of
    what it found back into the same thread; never writes to the tracker
    itself - see the module comment above for why.
    """
    from slack_sdk import WebClient

    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        tasks, method = extract_tasks(text, meeting_path=None)
        if not tasks:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                     text="Didn't find any clear action items in that message.")
            return

        existing = load_tracker(TRACKER_PATH)
        proposals = propose_merge(existing, tasks, date.today().isoformat())
        lines = [f"Found {len(proposals)} possible task(s) (extraction: {method}) - "
                 f"review in the PM Agent web UI before anything's saved:"]
        for p in proposals:
            tag = "UPDATE" if p["action"] == "update" else "NEW"
            row = p["row"]
            lines.append(f"\u2022 [{tag}] *{row['task']}* \u2014 {row['owner']} ({row['status']})")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines))
    except Exception as e:
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                     text=f"Something went wrong processing that: {e}")
        except Exception:
            pass  # if we can't even post the error, there's nothing more to do from here


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    if not SLACK_ENABLED:
        raise HTTPException(503, "Slack integration is not configured on this server "
                                  "(SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET not set).")

    body = await request.body()
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    signature = request.headers.get("x-slack-signature", "")
    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(401, "Invalid Slack signature.")

    payload = await request.json()

    # Slack's one-time handshake when you first configure the Events API URL.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    # Slack retries if it doesn't get a fast 200 - since we already fully
    # processed the first attempt (or are in the middle of it), just ack
    # retries without reprocessing.
    if request.headers.get("x-slack-retry-num"):
        return {"ok": True}

    if payload.get("type") == "event_callback":
        event = payload.get("event", {})
        is_plain_message = event.get("type") == "message" and not event.get("bot_id") and event.get("subtype") is None
        channel_allowed = not SLACK_NOTES_CHANNEL_ID or event.get("channel") == SLACK_NOTES_CHANNEL_ID
        if is_plain_message and channel_allowed:
            background_tasks.add_task(
                _process_slack_message, event.get("channel"), event.get("ts"), event.get("text", "")
            )

    return {"ok": True}


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
