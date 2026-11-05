"""Shared fixtures. Every test runs against a private copy of tests/seed_db.json.

- `db_path` (autouse): copies the seed DB into the test's tmp_path and points
  SCOOTER_DB_PATH at it. app.py reads SCOOTER_DB_PATH on every DB access, so this
  works no matter when `app` was imported.
- `client`: a Flask test client for app.py, with TESTING=True for that test only.
- `read_db`: returns the current contents of the test's DB file (to assert on what
  the app actually persisted).
- `_guard_real_db` (autouse): fails the test if the live scooter_db.json changes.
"""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DB = REPO_ROOT / "scooter_db.json"
SEED_DB = Path(__file__).resolve().parent / "seed_db.json"


def _fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture(autouse=True)
def _guard_real_db():
    before = _fingerprint(REAL_DB)
    yield
    assert _fingerprint(REAL_DB) == before, (
        f"{REAL_DB} changed during this test: tests must only touch the temp DB "
        "(use the `client` fixture; never point SCOOTER_DB_PATH at the real file). "
        "Restore it with: git restore scooter_db.json"
    )


@pytest.fixture(autouse=True)
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "scooter_db.json"
    shutil.copyfile(SEED_DB, path)
    monkeypatch.setenv("SCOOTER_DB_PATH", str(path))
    return path


@pytest.fixture
def read_db(db_path):
    def _read():
        return {s["id"]: s for s in json.loads(db_path.read_text())}
    return _read


@pytest.fixture
def client(monkeypatch):
    import app as app_module
    # TESTING=True makes unhandled exceptions propagate into the test with a traceback.
    # setitem restores the old value after each test; a test can override it with its own
    # monkeypatch.setitem(app.app.config, "TESTING", False) to see a real 500 response.
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    return app_module.app.test_client()


@pytest.fixture
def get_json(client):
    """GET `url`, assert the status code and a JSON content type, return the parsed body."""
    def _get(url, status):
        resp = client.get(url)
        body = resp.get_data(as_text=True)
        assert resp.status_code == status, f"GET {url}: {resp.status_code} {body}"
        assert resp.mimetype == "application/json", f"GET {url}: {resp.mimetype} {body}"
        return resp.get_json()
    return _get
