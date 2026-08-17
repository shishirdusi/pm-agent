"""
tracker.py
Loads/saves the task tracker CSV and merges newly-extracted tasks into it
without creating duplicates.

Dedupe strategy:
  A newly-extracted task is considered "the same task" as an existing row
  if they share the same workstream AND the task titles are similar enough
  (normalized text similarity above a threshold) - this catches cases
  where wording drifts slightly between meetings ("Update Registration
  Flow" vs "Move registration step to end of flow") but the underlying
  work item is clearly the same.

  Owner is intentionally NOT part of the match key: ownership sometimes
  shifts between meetings (e.g. work gets reassigned), and we want the
  agent to surface that as a possible update for the human to confirm,
  not silently create a second row.

  When a match is found, the row is UPDATED (status, priority, due date,
  blocker, follow-up flag, last_updated, and an appended note showing the
  change history) rather than duplicated. When no match is found, a new
  row is appended.

This module never writes to disk directly during merge - it returns a
proposed new state, which the human-review step then approves before
`save_tracker()` is called. That's the human-in-the-loop boundary.
"""

import csv
import difflib
import re
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

COLUMNS = [
    "workstream", "task", "description", "owner", "status", "priority",
    "due_date", "blocker", "last_updated", "follow_up_needed", "notes",
]

SIMILARITY_THRESHOLD = 0.55  # tuned for short PM-style task titles


@dataclass
class TrackerRow:
    workstream: str
    task: str
    description: str = ""
    owner: str = "TBD"
    status: str = "Not Started"
    priority: str = "Medium"
    due_date: str = ""
    blocker: str = ""
    last_updated: str = ""
    follow_up_needed: str = "No"
    notes: str = ""

    def as_row(self) -> dict:
        return asdict(self)


def load_tracker(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_tracker(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in COLUMNS})


def _title_similarity(a: str, b: str) -> float:
    # Punctuation/hyphens become spaces (not just stripped) so "Multi-URL" and "Multi URL"
    # normalize to the same tokens instead of accidentally merging into "multiurl".
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() if s else ""
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def _find_match(new_task: dict, existing_rows: list[dict]) -> int | None:
    best_idx, best_score = None, 0.0
    for i, row in enumerate(existing_rows):
        if row.get("workstream", "").strip().lower() != new_task.get("workstream", "").strip().lower():
            continue
        score = _title_similarity(row.get("task", ""), new_task.get("task", ""))
        if score > best_score:
            best_idx, best_score = i, score
    if best_idx is not None and best_score >= SIMILARITY_THRESHOLD:
        return best_idx
    return None


def propose_merge(existing_rows: list[dict], new_tasks: list[dict], meeting_date: str) -> list[dict]:
    """
    Returns a list of "review items", one per new_task, of the form:
      {"action": "add" | "update", "index": int|None, "row": dict, "diff": str}
    Nothing is written to the tracker here - this is the staged proposal
    that the human reviews next.
    """
    proposals = []
    rows_copy = list(existing_rows)

    for new_task in new_tasks:
        row = {c: new_task.get(c, "") for c in COLUMNS if c != "last_updated"}
        row["last_updated"] = meeting_date

        match_idx = _find_match(new_task, rows_copy)
        if match_idx is None:
            proposals.append({"action": "add", "index": None, "row": row, "new_only_row": row, "diff": None})
        else:
            old = rows_copy[match_idx]
            changes = []
            for field_name in ("status", "owner", "priority", "due_date", "blocker", "follow_up_needed"):
                if old.get(field_name, "") != row.get(field_name, ""):
                    changes.append(f"{field_name}: '{old.get(field_name,'')}' -> '{row.get(field_name,'')}'")
            merged = dict(old)
            merged.update(row)
            # preserve a short history trail in notes instead of clobbering it
            if changes:
                history_note = f"[{meeting_date}] " + "; ".join(changes)
                merged["notes"] = (old.get("notes", "").strip() + (" | " if old.get("notes", "").strip() else "") + history_note)
            proposals.append({
                "action": "update",
                "index": match_idx,
                "row": merged,
                "new_only_row": row,  # the new task's own data, undiluted by the old row -
                                      # used if the reviewer decides this was a false-positive match
                "diff": "; ".join(changes) if changes else "no field changes (duplicate mention)",
            })

    return proposals
