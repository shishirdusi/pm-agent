"""
Tests for sheets_writer.py. Google Sheets itself is never actually contacted -
gspread and the Google auth module are replaced with mocks via sys.modules, so
these verify the LOGIC (correct skip behavior, correct data shape sent to the
sheet) without needing real credentials or a real spreadsheet. This is not a
substitute for testing against an actual Google Sheet at least once before
relying on this in production - see the module docstring in sheets_writer.py
for that one-time setup - but it does catch real bugs (e.g. a column list
mismatch) without needing that setup just to run the test suite.

Run with: pytest tests/test_sheets_writer.py -v
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import COLUMNS


def make_row(**overrides):
    row = {c: "" for c in COLUMNS}
    row.update({"workstream": "Test WS", "task": "Test task", "owner": "Someone", "status": "Not Started"})
    row.update(overrides)
    return row


def test_skips_cleanly_when_not_configured(monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("PM_AGENT_SHEET_ID", raising=False)
    import sheets_writer
    sheets_writer.write_to_google_sheet([make_row()])
    out = capsys.readouterr().out
    assert "Skipped" in out
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in out


def test_writes_correct_header_and_row_shape_to_the_sheet(monkeypatch, tmp_path):
    fake_creds_file = tmp_path / "fake_creds.json"
    fake_creds_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(fake_creds_file))
    monkeypatch.setenv("PM_AGENT_SHEET_ID", "fake_sheet_id_123")

    mock_worksheet = MagicMock()
    mock_sheet = MagicMock()
    mock_sheet.worksheet.return_value = mock_worksheet
    mock_gspread_client = MagicMock()
    mock_gspread_client.open_by_key.return_value = mock_sheet

    mock_gspread_module = MagicMock()
    mock_gspread_module.authorize.return_value = mock_gspread_client
    mock_gspread_module.WorksheetNotFound = Exception  # so `except gspread.WorksheetNotFound` is valid

    mock_google_auth_module = MagicMock()
    mock_google_auth_module.Credentials.from_service_account_file.return_value = MagicMock()

    with patch.dict(sys.modules, {
        "gspread": mock_gspread_module,
        "google.oauth2.service_account": mock_google_auth_module,
    }):
        import sheets_writer
        import importlib
        importlib.reload(sheets_writer)  # pick up the patched sys.modules for this test's local imports
        rows = [make_row(task="Task One"), make_row(task="Task Two", status="Blocked")]
        sheets_writer.write_to_google_sheet(rows)

    mock_gspread_client.open_by_key.assert_called_once_with("fake_sheet_id_123")
    mock_worksheet.clear.assert_called_once()

    written_values = mock_worksheet.update.call_args[0][0]
    header_row = written_values[0]
    assert header_row == [c.replace("_", " ").title() for c in COLUMNS], \
        "sheet header must match tracker.COLUMNS exactly, or CSV and Sheet output would silently diverge"
    assert len(written_values) == 3  # header + 2 data rows
    assert written_values[1][COLUMNS.index("task")] == "Task One"
    assert written_values[2][COLUMNS.index("status")] == "Blocked"


def test_creates_worksheet_if_it_does_not_exist_yet(monkeypatch, tmp_path):
    fake_creds_file = tmp_path / "fake_creds.json"
    fake_creds_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(fake_creds_file))
    monkeypatch.setenv("PM_AGENT_SHEET_ID", "fake_sheet_id_123")

    class FakeWorksheetNotFound(Exception):
        pass

    mock_sheet = MagicMock()
    mock_sheet.worksheet.side_effect = FakeWorksheetNotFound()  # simulate "Task Tracker" tab not existing yet
    mock_new_worksheet = MagicMock()
    mock_sheet.add_worksheet.return_value = mock_new_worksheet

    mock_gspread_client = MagicMock()
    mock_gspread_client.open_by_key.return_value = mock_sheet
    mock_gspread_module = MagicMock()
    mock_gspread_module.authorize.return_value = mock_gspread_client
    mock_gspread_module.WorksheetNotFound = FakeWorksheetNotFound

    mock_google_auth_module = MagicMock()
    mock_google_auth_module.Credentials.from_service_account_file.return_value = MagicMock()

    with patch.dict(sys.modules, {
        "gspread": mock_gspread_module,
        "google.oauth2.service_account": mock_google_auth_module,
    }):
        import sheets_writer
        import importlib
        importlib.reload(sheets_writer)
        sheets_writer.write_to_google_sheet([make_row()])

    mock_sheet.add_worksheet.assert_called_once()
    mock_new_worksheet.update.assert_called_once()
