import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "test-token"
ADMIN = "test-admin-password"


@pytest.fixture
def sample_report():
    report = json.loads((FIXTURES / "sample_report.json").read_text(encoding="utf-8"))
    report["collector_version"] = (Path(__file__).parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    return report


@pytest.fixture
def web(tmp_path, monkeypatch):
    """Flask test client whose state lives in a temporary directory."""
    import app as web_app
    monkeypatch.setattr(web_app, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(web_app, "REFRESH_FILE", tmp_path / "refresh.json")
    monkeypatch.setattr(web_app, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(web_app, "LOG_FILE", tmp_path / "job.log")
    monkeypatch.setattr(web_app, "PROGRESS_FILE", tmp_path / "progress.json")
    monkeypatch.setattr(web_app, "TOKEN", TOKEN)
    monkeypatch.setattr(web_app, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(web_app.time, "sleep", lambda _seconds: None)  # no delay on wrong passwords
    return web_app.app.test_client()


@pytest.fixture
def token_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def admin_headers():
    return {"X-Admin-Password": ADMIN}
