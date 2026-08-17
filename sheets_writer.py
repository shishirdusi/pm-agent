"""
sheets_writer.py
Pushes the tracker rows to a real Google Sheet, using a service account.

This is the production path for deliverable #4 ("output should be
reflected in a Google Sheet"). It's optional at runtime: if credentials
aren't configured, run_pipeline.py simply skips this and the CSV in
output/task_tracker.csv remains the source of truth (which is also a
fully valid deliverable format per the brief - "A Google Sheet or CSV
output format").

Setup (one-time, not needed to run the CSV-only demo):
  1. Create a Google Cloud service account, enable the Sheets API, and
     download its JSON key.
  2. Share your target Google Sheet with the service account's email
     (found inside the JSON key) as an Editor.
  3. Set two environment variables before running with --write-sheet:
       GOOGLE_SERVICE_ACCOUNT_JSON = path to the downloaded key file
       PM_AGENT_SHEET_ID           = the target spreadsheet's ID (from its URL)
  4. pip install gspread google-auth

Behavior once configured:
  - Reads the existing sheet, matches on (workstream, task) the same way
    tracker.py does for the CSV, and does a full-sheet rewrite of the
    tracker tab so it always mirrors output/task_tracker.csv exactly -
    no separate dedupe logic to keep in sync.
"""

import os

from tracker import COLUMNS


def write_to_google_sheet(rows: list[dict], sheet_id: str | None = None,
                           worksheet_name: str = "Task Tracker") -> None:
    sheet_id = sheet_id or os.environ.get("PM_AGENT_SHEET_ID")
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not sheet_id or not creds_path:
        print("[sheets_writer] Skipped: set GOOGLE_SERVICE_ACCOUNT_JSON and "
              "PM_AGENT_SHEET_ID to enable live Google Sheets writes. "
              "The CSV tracker is up to date and can be imported into "
              "Sheets manually (File > Import) in the meantime.")
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("[sheets_writer] gspread / google-auth not installed "
              "(pip install gspread google-auth). Skipping live write.")
        return

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(creds)

    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=str(len(rows) + 10), cols=str(len(COLUMNS)))

    ws.clear()
    header = [c.replace("_", " ").title() for c in COLUMNS]
    values = [header] + [[row.get(c, "") for c in COLUMNS] for row in rows]
    ws.update(values)
    print(f"[sheets_writer] Wrote {len(rows)} rows to Google Sheet '{worksheet_name}' "
          f"(https://docs.google.com/spreadsheets/d/{sheet_id})")
