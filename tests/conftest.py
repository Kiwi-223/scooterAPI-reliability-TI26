"""Shared fixtures. Every test runs against a private copy of tests/seed_db.json.

- `db_path` (autouse): copies the seed DB into the test's tmp_path and points
  SCOOTER_DB_PATH at it. app.py reads SCOOTER_DB_PATH on every DB access, so this
  works no matter when `app` was imported.
- `client`: a Flask test client for app.py, with TESTING=True for that test only.
- `read_db`: returns the current contents of the test's DB file (to assert on what
  the app actually persisted).
- `_guard_real_db` (autouse): fails the test if the live scooter_db.json changes.
- `records_path` (autouse): points SCOOTER_RECORDS_PATH at <tmp_path>/records.jsonl.
- `read_records`: returns the test's request records as a list of dicts (one per line).
- `_guard_real_records` (autouse): fails the test if the live data/records.jsonl changes.
- `ledger_path` (autouse): points SCOOTER_PAYMENTS_LEDGER_PATH at <tmp_path>/payments_ledger.jsonl
  and clears every PAYMENT_* / ENABLE_FAULT_INJECTION variable, so the gateway starts empty
  with no faults whatever the developer's shell exports.
- `read_ledger`: returns the test's ledger entries as a list of dicts.
- `_guard_real_ledger` (autouse): fails the test if the live data/payments_ledger.jsonl changes.
"""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DB = REPO_ROOT / "scooter_db.json"
REAL_RECORDS = REPO_ROOT / "data" / "records.jsonl"
REAL_LEDGER = REPO_ROOT / "data" / "payments_ledger.jsonl"
PAYMENT_ENV = ("PAYMENT_FAULT", "PAYMENT_SEED", "PAYMENT_FAULT_RATE", "PAYMENT_TIMEOUT_DELAY_MS",
               "ENABLE_FAULT_INJECTION")
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
def _guard_real_records():
    before = _fingerprint(REAL_RECORDS)
    yield
    assert _fingerprint(REAL_RECORDS) == before, (
        f"{REAL_RECORDS} changed during this test: tests must only write to the temp "
        "records file (the autouse `records_path` fixture sets SCOOTER_RECORDS_PATH)."
    )


@pytest.fixture(autouse=True)
def _guard_real_ledger():
    before = _fingerprint(REAL_LEDGER)
    yield
    assert _fingerprint(REAL_LEDGER) == before, (
        f"{REAL_LEDGER} changed during this test: tests must only use the temp ledger "
        "(the autouse `ledger_path` fixture sets SCOOTER_PAYMENTS_LEDGER_PATH)."
    )


@pytest.fixture(autouse=True)
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "payments_ledger.jsonl"
    monkeypatch.setenv("SCOOTER_PAYMENTS_LEDGER_PATH", str(path))
    for name in PAYMENT_ENV:
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def read_ledger(ledger_path):
    def _read():
        if not ledger_path.exists():
            return []
        return [json.loads(line) for line in ledger_path.read_text().split("\n") if line]
    return _read


@pytest.fixture(autouse=True)
def records_path(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    monkeypatch.setenv("SCOOTER_RECORDS_PATH", str(path))
    return path


@pytest.fixture
def read_records(records_path):
    def _read():
        if not records_path.exists():
            return []
        # Split on "\n" only: str.splitlines() also breaks on U+2028, U+0085 etc., which a
        # record could contain if it weren't ASCII-escaped (records use ensure_ascii=True).
        return [json.loads(line) for line in records_path.read_text().split("\n") if line]
    return _read


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
    # TESTING=True for this test only (setitem restores it afterwards); a test can override
    # it with monkeypatch.setitem(app.app.config, "TESTING", False). Since PLAN PR 7 the
    # app's Exception catch-all turns unhandled exceptions into a JSON internal_error 500 in
    # both modes; the traceback is logged ("unhandled exception"), so use caplog to see it.
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
