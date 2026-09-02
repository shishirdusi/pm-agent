"""
Tests for the Slack integration in server.py. Uses FastAPI's TestClient to drive
the real app in-process (no real network, no real Slack) - but the HMAC
signatures used ARE genuinely computed with Slack's real signing algorithm, so
_verify_slack_signature is tested against real valid/invalid signatures, not
just mocked around. The one thing genuinely untestable without a live Slack
app is the round trip through Slack's actual servers - see README.md for the
manual setup steps that cover that part.

Run with: pytest tests/test_slack.py -v
"""
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

TEST_SIGNING_SECRET = "test-signing-secret-abc123"
TEST_BOT_TOKEN = "xoxb-test-token"


def sign(body: bytes, secret: str = TEST_SIGNING_SECRET, timestamp: str = None) -> tuple[str, str]:
    """Computes a REAL, valid Slack request signature - the exact algorithm
    Slack itself uses - so tests exercise the real verification logic."""
    ts = timestamp or str(int(time.time()))
    basestring = f"v0:{ts}:{body.decode('utf-8')}"
    sig = "v0=" + hmac.new(secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return ts, sig


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", TEST_SIGNING_SECRET)
    monkeypatch.delenv("SLACK_NOTES_CHANNEL_ID", raising=False)
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.chdir(Path(__file__).parent.parent)
    (tmp_path / "tracker.csv").write_text("")

    import server
    import importlib
    importlib.reload(server)  # pick up the patched env vars for SLACK_ENABLED etc.
    server.TRACKER_PATH = tmp_path / "tracker.csv"
    return TestClient(server.app)


def test_missing_signature_headers_rejected(client):
    body = json.dumps({"type": "event_callback"}).encode()
    resp = client.post("/slack/events", content=body)
    assert resp.status_code == 401


def test_invalid_signature_rejected(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    ts, _ = sign(body)
    resp = client.post("/slack/events", content=body, headers={
        "x-slack-request-timestamp": ts,
        "x-slack-signature": "v0=0000000000000000000000000000000000000000000000000000000000000000",
    })
    assert resp.status_code == 401


def test_tampered_body_rejected(client):
    """Signature was computed for ONE body, but a different body is sent - must fail."""
    original_body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    ts, sig = sign(original_body)
    tampered_body = json.dumps({"type": "url_verification", "challenge": "SOMETHING ELSE"}).encode()
    resp = client.post("/slack/events", content=tampered_body, headers={
        "x-slack-request-timestamp": ts, "x-slack-signature": sig,
    })
    assert resp.status_code == 401


def test_replay_attack_old_timestamp_rejected(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    old_timestamp = str(int(time.time()) - 60 * 10)  # 10 minutes old
    ts, sig = sign(body, timestamp=old_timestamp)
    resp = client.post("/slack/events", content=body, headers={
        "x-slack-request-timestamp": ts, "x-slack-signature": sig,
    })
    assert resp.status_code == 401


def test_valid_signature_url_verification_returns_challenge(client):
    body = json.dumps({"type": "url_verification", "challenge": "expected-challenge-value"}).encode()
    ts, sig = sign(body)
    resp = client.post("/slack/events", content=body, headers={
        "x-slack-request-timestamp": ts, "x-slack-signature": sig,
    })
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "expected-challenge-value"}


def test_valid_message_event_triggers_extraction_and_posts_reply(client, monkeypatch):
    mock_client = MagicMock()
    with patch("slack_sdk.WebClient", return_value=mock_client):
        payload = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "ts": "1699999999.000100",
                "text": "[Priya Nair] Fix login bug: users getting logged out randomly.",
            },
        }
        body = json.dumps(payload).encode()
        ts, sig = sign(body)
        resp = client.post("/slack/events", content=body, headers={
            "x-slack-request-timestamp": ts, "x-slack-signature": sig,
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    mock_client.chat_postMessage.assert_called_once()
    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C123"
    assert call_kwargs["thread_ts"] == "1699999999.000100"
    assert "Fix login bug" in call_kwargs["text"]
    assert "Priya Nair" in call_kwargs["text"]


def test_bot_messages_are_ignored_to_prevent_reply_loops(client):
    mock_client = MagicMock()
    with patch("slack_sdk.WebClient", return_value=mock_client):
        payload = {
            "type": "event_callback",
            "event": {
                "type": "message", "channel": "C123", "ts": "1699999999.000100",
                "text": "Found 1 possible task...", "bot_id": "B0SOMEBOT",
            },
        }
        body = json.dumps(payload).encode()
        ts, sig = sign(body)
        client.post("/slack/events", content=body, headers={
            "x-slack-request-timestamp": ts, "x-slack-signature": sig,
        })
    mock_client.chat_postMessage.assert_not_called()


def test_message_edits_and_deletes_are_ignored_not_just_plain_bot_messages(client):
    """subtype is set for things like message_changed/message_deleted - only
    plain new messages (subtype is None) should trigger extraction."""
    mock_client = MagicMock()
    with patch("slack_sdk.WebClient", return_value=mock_client):
        payload = {
            "type": "event_callback",
            "event": {
                "type": "message", "channel": "C123", "ts": "1699999999.000100",
                "text": "edited text", "subtype": "message_changed",
            },
        }
        body = json.dumps(payload).encode()
        ts, sig = sign(body)
        client.post("/slack/events", content=body, headers={
            "x-slack-request-timestamp": ts, "x-slack-signature": sig,
        })
    mock_client.chat_postMessage.assert_not_called()


def test_retry_requests_are_acked_without_reprocessing(client):
    mock_client = MagicMock()
    with patch("slack_sdk.WebClient", return_value=mock_client):
        payload = {
            "type": "event_callback",
            "event": {"type": "message", "channel": "C123", "ts": "1699999999.000100", "text": "some notes"},
        }
        body = json.dumps(payload).encode()
        ts, sig = sign(body)
        resp = client.post("/slack/events", content=body, headers={
            "x-slack-request-timestamp": ts, "x-slack-signature": sig,
            "x-slack-retry-num": "1",
        })
    assert resp.status_code == 200
    mock_client.chat_postMessage.assert_not_called()


def test_channel_restriction_when_configured(client, monkeypatch):
    monkeypatch.setenv("SLACK_NOTES_CHANNEL_ID", "C_ALLOWED_ONLY")
    import server
    import importlib
    importlib.reload(server)
    from fastapi.testclient import TestClient as TC
    restricted_client = TC(server.app)

    mock_client = MagicMock()
    with patch("slack_sdk.WebClient", return_value=mock_client):
        payload = {
            "type": "event_callback",
            "event": {"type": "message", "channel": "C_SOME_OTHER_CHANNEL",
                       "ts": "1699999999.000100", "text": "notes here"},
        }
        body = json.dumps(payload).encode()
        ts, sig = sign(body)
        restricted_client.post("/slack/events", content=body, headers={
            "x-slack-request-timestamp": ts, "x-slack-signature": sig,
        })
    mock_client.chat_postMessage.assert_not_called()


def test_slack_endpoint_exempt_from_basic_auth(client, monkeypatch):
    """Even when APP_USERNAME/APP_PASSWORD are set for the browser UI, /slack/events
    must still work with just a valid Slack signature - Slack can't provide a
    username/password."""
    monkeypatch.setenv("APP_USERNAME", "someuser")
    monkeypatch.setenv("APP_PASSWORD", "somepass")
    import server
    import importlib
    importlib.reload(server)
    from fastapi.testclient import TestClient as TC
    auth_client = TC(server.app)

    body = json.dumps({"type": "url_verification", "challenge": "xyz"}).encode()
    ts, sig = sign(body)
    resp = auth_client.post("/slack/events", content=body, headers={
        "x-slack-request-timestamp": ts, "x-slack-signature": sig,
    })
    assert resp.status_code == 200, "should NOT require HTTP Basic auth credentials"
    assert resp.json() == {"challenge": "xyz"}


def test_disabled_when_not_configured(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    import server
    import importlib
    importlib.reload(server)
    from fastapi.testclient import TestClient as TC
    disabled_client = TC(server.app)

    resp = disabled_client.post("/slack/events", content=b'{"type":"url_verification"}')
    assert resp.status_code == 503
