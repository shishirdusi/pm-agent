"""
run_pipeline.py
The end-to-end flow for ONE meeting note:

    Meeting Notes -> Task Extraction -> Owner/Status/Blocker Detection
                   -> Human Review -> Tracker (CSV / Google Sheet) Update

Usage (interactive, real use):
    python run_pipeline.py --notes sample_meetings/2026-07-29_sanio-daily-standup.txt \
                            --meeting-date 2026-07-29

Also supports writing to Google Sheets instead of / in addition to CSV, if
gspread + a service-account credentials file are configured - see
sheets_writer.py. This is optional and CSV always works regardless.
"""

import argparse
from pathlib import Path

from extractor import extract_tasks
from tracker import COLUMNS, load_tracker, save_tracker, propose_merge
from review import review_proposals, apply_decisions

DEFAULT_TRACKER = Path(__file__).parent / "output" / "task_tracker.csv"


def run(notes_path: Path, meeting_date: str, tracker_path: Path = DEFAULT_TRACKER,
        interactive: bool = True, scripted_decisions=None, write_sheet: bool = False):
    meeting_text = notes_path.read_text(encoding="utf-8")

    tasks, method = extract_tasks(meeting_text, meeting_path=str(notes_path))
    print(f"[extractor] {len(tasks)} task(s) extracted via '{method}' from {notes_path.name}")

    existing_rows = load_tracker(tracker_path)
    proposals = propose_merge(existing_rows, tasks, meeting_date)

    n_new = sum(1 for p in proposals if p["action"] == "add")
    n_upd = sum(1 for p in proposals if p["action"] == "update")
    print(f"[tracker]   {n_new} new task(s), {n_upd} update(s) to existing tasks proposed")

    if interactive:
        decisions = review_proposals(proposals)
    else:
        from review import simulate_review
        decisions = simulate_review(proposals, scripted_decisions)

    n_ignored = sum(1 for d in decisions if d.action == "ignore")
    n_edited = sum(1 for d in decisions if d.action == "edit")
    print(f"[review]    {len(decisions) - n_ignored} approved ({n_edited} edited), {n_ignored} ignored")

    new_rows = apply_decisions(proposals, decisions, existing_rows)
    save_tracker(tracker_path, new_rows)
    print(f"[tracker]   saved -> {tracker_path}  ({len(new_rows)} total task rows)")

    if write_sheet:
        from sheets_writer import write_to_google_sheet
        write_to_google_sheet(new_rows)

    return new_rows, proposals, decisions


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the PM Agent on one meeting note.")
    ap.add_argument("--notes", required=True, type=Path, help="Path to meeting notes (.txt)")
    ap.add_argument("--meeting-date", required=True, help="YYYY-MM-DD, used for last_updated")
    ap.add_argument("--tracker", type=Path, default=DEFAULT_TRACKER, help="Path to tracker CSV")
    ap.add_argument("--write-sheet", action="store_true",
                     help="Also push the result to Google Sheets (needs credentials configured)")
    args = ap.parse_args()

    run(args.notes, args.meeting_date, args.tracker, interactive=True, write_sheet=args.write_sheet)
