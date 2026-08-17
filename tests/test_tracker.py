"""
Tests for tracker.py: the dedupe/merge logic and the human-review apply step.

Run with: pytest tests/test_tracker.py -v
(or just `pytest` from the project root to run everything)
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import (
    propose_merge, load_tracker, save_tracker, _title_similarity, _find_match, COLUMNS,
    SIMILARITY_THRESHOLD,
)
from review import ReviewDecision, apply_decisions


def make_task(workstream="Catalog Agents - Metering", task="Build metering module", **overrides):
    base = {
        "workstream": workstream, "task": task, "description": "desc", "owner": "Saumya Phadkar",
        "status": "In Progress", "priority": "High", "due_date": "", "blocker": "",
        "follow_up_needed": "No", "notes": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- propose_merge

def test_new_task_on_empty_tracker_is_an_add():
    proposals = propose_merge([], [make_task()], "2026-07-29")
    assert len(proposals) == 1
    assert proposals[0]["action"] == "add"
    assert proposals[0]["index"] is None
    assert proposals[0]["row"]["last_updated"] == "2026-07-29"


def test_identical_task_in_same_workstream_is_an_update_not_a_duplicate():
    existing = [make_task(status="In Progress", last_updated="2026-07-29")]
    new_task = make_task(status="Completed")
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    assert len(proposals) == 1
    assert proposals[0]["action"] == "update"
    assert proposals[0]["index"] == 0
    assert proposals[0]["row"]["status"] == "Completed"
    assert "status:" in proposals[0]["diff"]  # diff is a "; "-joined string, not a list


def test_different_workstream_never_matches_even_with_identical_title():
    existing = [make_task(workstream="Workstream A")]
    new_task = make_task(workstream="Workstream B")  # same title, different workstream
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    assert proposals[0]["action"] == "add", "must not cross workstream boundaries when matching"


def test_dissimilar_titles_in_same_workstream_do_not_match():
    existing = [make_task(task="Fix login bug")]
    new_task = make_task(task="Write Q3 marketing plan")
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    assert proposals[0]["action"] == "add"


def test_update_preserves_old_notes_and_appends_history():
    existing = [make_task(notes="original context", status="Not Started")]
    new_task = make_task(status="Blocked", blocker="waiting on API key")
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    merged_notes = proposals[0]["row"]["notes"]
    assert "original context" in merged_notes
    assert "2026-08-02" in merged_notes
    assert "status:" in merged_notes


def test_no_field_changes_still_matches_but_reports_no_diff():
    existing = [make_task()]
    new_task = make_task()  # identical in every tracked field
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    assert proposals[0]["action"] == "update"
    assert proposals[0]["diff"] == "no field changes (duplicate mention)"


def test_owner_change_alone_is_still_treated_as_an_update_not_ignored():
    # Ownership sometimes shifts between meetings - the match key is workstream+title,
    # not owner, specifically so a reassignment gets surfaced for review rather than
    # silently creating a second row.
    existing = [make_task(owner="Bindu Achalla")]
    new_task = make_task(owner="Yatharth Srivastava")
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    assert proposals[0]["action"] == "update"
    assert "owner:" in proposals[0]["diff"]


# ---------------------------------------------------------------- similarity helper

def test_title_similarity_identical_strings_is_one():
    assert _title_similarity("Fix login bug", "Fix login bug") == 1.0


def test_title_similarity_unrelated_strings_is_low():
    assert _title_similarity("Fix login bug", "Plan the holiday party") < 0.3


def test_title_similarity_ignores_case_and_punctuation():
    assert _title_similarity("Fix Login-Bug!", "fix login bug") == 1.0


def test_title_similarity_short_near_identical_titles_can_overmatch():
    # Known, documented limitation: bigram/sequence similarity on very short titles is
    # unreliable - "Task A" vs "Task B" differ by one character but score deceptively
    # high. Real PM task titles are descriptive phrases, not short placeholders, so this
    # mainly matters as a reminder of why the review step (approve/split) exists rather
    # than trusting the auto-match outright. See README "What I'd improve".
    score = _title_similarity("Task A", "Task B")
    assert score > SIMILARITY_THRESHOLD, (
        "this asserts the KNOWN limitation exists, so if the matching algorithm changes "
        "and this stops overmatching, this test should be revisited rather than just fixed"
    )


# ---------------------------------------------------------------- apply_decisions (review)

def test_approve_add_appends_new_row():
    proposals = propose_merge([], [make_task()], "2026-07-29")
    decisions = [ReviewDecision(0, "approve")]
    result = apply_decisions(proposals, decisions, [])
    assert len(result) == 1
    assert result[0]["task"] == "Build metering module"


def test_ignore_drops_the_task_entirely():
    proposals = propose_merge([], [make_task()], "2026-07-29")
    decisions = [ReviewDecision(0, "ignore")]
    result = apply_decisions(proposals, decisions, [])
    assert result == []


def test_edit_overrides_fields_before_saving():
    proposals = propose_merge([], [make_task()], "2026-07-29")
    decisions = [ReviewDecision(0, "edit", {"owner": "Corrected Name", "priority": "Low"})]
    result = apply_decisions(proposals, decisions, [])
    assert result[0]["owner"] == "Corrected Name"
    assert result[0]["priority"] == "Low"
    assert result[0]["task"] == "Build metering module"  # untouched fields survive


def test_split_adds_a_new_row_instead_of_overwriting_the_auto_matched_one():
    # This is the core "reviewer catches a false-positive auto-match" scenario.
    existing = [make_task(task="Integrate five products into multi-URL flow", owner="Yatharth Srivastava")]
    new_task = make_task(task="Five-product input for multi-URL feature", owner="Bindu Achalla")
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    assert proposals[0]["action"] == "update", "sanity check: these titles should auto-match"

    decisions = [ReviewDecision(0, "split")]
    result = apply_decisions(proposals, decisions, existing)

    assert len(result) == 2, "split must add a new row, not overwrite the existing one"
    assert result[0]["owner"] == "Yatharth Srivastava", "the original row must be untouched"
    assert result[1]["owner"] == "Bindu Achalla", "the split-off row keeps the NEW task's own data"


def test_split_row_does_not_inherit_the_old_rows_notes():
    existing = [make_task(task="Review metering docs", notes="Saumya's original context")]
    new_task = make_task(task="Review metering documentation", notes="Sandeep's separate task", owner="Sandeep Chakradhar")
    proposals = propose_merge(existing, [new_task], "2026-08-02")
    decisions = [ReviewDecision(0, "split")]
    result = apply_decisions(proposals, decisions, existing)
    assert result[1]["notes"] == "Sandeep's separate task"
    assert "Saumya's original context" not in result[1]["notes"]


def test_multiple_proposals_mixed_decisions():
    existing = [make_task(task="Build metering module for outcome-based pricing", status="Not Started")]
    new_tasks = [
        make_task(task="Build metering module for outcome-based pricing", status="Completed"),  # -> update
        make_task(task="Migrate Langfuse tracing to the internal Sano instance", owner="TBD"),   # -> add
        make_task(task="Upgrade Chloro subscription for more concurrent jobs", owner="TBD"),     # -> add, ignored
    ]
    proposals = propose_merge(existing, new_tasks, "2026-08-05")
    assert [p["action"] for p in proposals] == ["update", "add", "add"], \
        "sanity check: these three titles are distinct enough that only the exact repeat should match"
    decisions = [
        ReviewDecision(0, "approve"),
        ReviewDecision(1, "edit", {"owner": "Rohit Adiga"}),
        ReviewDecision(2, "ignore"),
    ]
    result = apply_decisions(proposals, decisions, existing)
    assert len(result) == 2
    assert result[0]["status"] == "Completed"
    assert result[1]["task"] == "Migrate Langfuse tracing to the internal Sano instance"
    assert result[1]["owner"] == "Rohit Adiga"


# ---------------------------------------------------------------- CSV round-trip

def test_save_and_load_tracker_round_trip(tmp_path):
    rows = [make_task(task="A"), make_task(task="B", status="Blocked", blocker="waiting on review")]
    path = tmp_path / "tracker.csv"
    save_tracker(path, rows)

    loaded = load_tracker(path)
    assert len(loaded) == 2
    assert loaded[0]["task"] == "A"
    assert loaded[1]["blocker"] == "waiting on review"
    assert list(loaded[0].keys()) == COLUMNS


def test_load_tracker_missing_file_returns_empty_list(tmp_path):
    assert load_tracker(tmp_path / "does_not_exist.csv") == []


def test_save_tracker_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "tracker.csv"
    save_tracker(path, [make_task()])
    assert path.exists()
