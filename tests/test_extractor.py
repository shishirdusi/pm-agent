"""
Tests for extractor.py. These deliberately avoid any real network calls (no
ANTHROPIC_API_KEY is set during tests), so they exercise the cached-demo and
rule-based-fallback tiers - the same tiers that run when no API key is
configured on your machine.

Run with: pytest tests/test_extractor.py -v
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from extractor import extract_tasks, _rule_based_extraction, TASK_SCHEMA_FIELDS


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Force every test in this file down the non-network path, regardless of the
    environment the tests happen to run in."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_extraction_uses_cache_for_known_sample_meeting():
    meeting_path = Path(__file__).parent.parent / "sample_meetings" / "2026-07-29_sanio-daily-standup.txt"
    text = meeting_path.read_text()
    tasks, method = extract_tasks(text, meeting_path=str(meeting_path))
    assert method == "cached-demo"
    assert len(tasks) > 0
    for t in tasks:
        assert set(TASK_SCHEMA_FIELDS).issubset(t.keys())


def test_extraction_falls_back_to_rule_based_for_unknown_text():
    text = "Just some unrelated chit chat with no bracket-style next steps."
    tasks, method = extract_tasks(text, meeting_path="not_a_real_file.txt")
    assert method == "rule-based-fallback"


def test_extraction_with_no_meeting_path_skips_cache_entirely():
    text = "[Priya Nair] Fix login bug: users getting logged out randomly."
    tasks, method = extract_tasks(text, meeting_path=None)
    assert method == "rule-based-fallback"
    assert len(tasks) == 1
    assert tasks[0]["owner"] == "Priya Nair"


def test_rule_based_extraction_picks_up_bracket_pattern():
    text = (
        "Next steps:\n"
        "[Arjun Mehta] Update pricing page: reflect the new tier structure.\n"
        "[Rohit Adiga] Send env file: needed to unblock the data-cutoff fix.\n"
    )
    tasks = _rule_based_extraction(text)
    assert len(tasks) == 2
    assert tasks[0]["owner"] == "Arjun Mehta"
    assert tasks[0]["task"] == "Update pricing page"
    assert "reflect the new tier structure" in tasks[0]["description"]


def test_rule_based_extraction_flags_group_ownership_as_tbd():
    text = "[the group] Deploy work: ship all assigned tasks by end of day.\n"
    tasks = _rule_based_extraction(text)
    assert tasks[0]["owner"] == "TBD"
    assert tasks[0]["follow_up_needed"] == "Yes"


def test_rule_based_extraction_on_empty_text_returns_empty_list():
    assert _rule_based_extraction("") == []
    assert _rule_based_extraction("No action items here at all, just chat.") == []


def test_all_cached_meetings_have_valid_schema():
    cache_dir = Path(__file__).parent.parent / "cache"
    import json
    for cache_file in cache_dir.glob("*.json"):
        tasks = json.loads(cache_file.read_text())
        assert isinstance(tasks, list) and len(tasks) > 0, f"{cache_file.name} should be a non-empty list"
        for t in tasks:
            missing = set(TASK_SCHEMA_FIELDS) - set(t.keys())
            assert not missing, f"{cache_file.name} has a task missing fields: {missing}"
            assert t["status"] in {"Not Started", "In Progress", "Pending Review", "Blocked", "Completed"}, \
                f"{cache_file.name}: unexpected status '{t['status']}'"
            assert t["follow_up_needed"] in {"Yes", "No"}
