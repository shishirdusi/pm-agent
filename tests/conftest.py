"""
conftest.py
Applies to every test in tests/ automatically (pytest auto-discovers conftest.py).

Forces ANTHROPIC_API_KEY off for the whole test suite by default, regardless of
what's actually set in the developer's shell or .env. Without this, running
`pytest` in a terminal where you've already exported a real key for other work
would make tests/test_tracker.py's dedupe tests (and test_extractor.py's
extraction tests) silently start making real, billed API calls - turning fast,
free, deterministic tests into slow, flaky, costly ones. Tests that specifically
want to exercise the "a key IS available" code path set it back explicitly
within that test via monkeypatch.setenv(...).
"""
import pytest


@pytest.fixture(autouse=True)
def no_real_api_calls_by_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
