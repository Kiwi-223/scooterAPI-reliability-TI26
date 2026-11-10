"""Request records (records.py, AGENTS.md "Request records"): one JSON line per request."""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import app as app_module
import records
from failures import FailureKind
from test_failure_kinds import TRIGGERS

REPO = Path(__file__).resolve().parent.parent
TOP_LEVEL_KEYS = {
    "schema", "ts", "request_id", "method", "path", "params", "params_truncated", "scooter_id",
    "decisions", "scooter_before", "scooter_after", "scooter_in_memory", "db_write", "status",
    "error", "duration_ms", "git_commit", "git_dirty", "git_dirty_paths", "pid",
}
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
# The seed is legacy-shaped (no "reservation" key), so 33 loads with a synthesised legacy
# reservation (app.legacy_reservation) and records show it that way.
LEGACY_33 = {"reservation_id": "legacy-33", "started_at": None, "start_request_id": None,
             "payment_state": "active", "idempotency_key": "legacy-33", "legacy": True}
SEED = {
    "18": {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": False, "reservation": None},
    "33": {"id": "33", "lat": 24.0, "lng": -28.0, "is_reserved": True, "reservation": LEGACY_33},
}


def reserved_18(snapshot):
    """SEED 18 reserved, with the (random) reservation the snapshot carries, after checking it's real."""
    reservation = snapshot["reservation"]
    assert isinstance(reservation, dict) and re.fullmatch(r"[0-9a-f]{32}", reservation["reservation_id"])
    return dict(SEED["18"], is_reserved=True, reservation=reservation)


def only_record(read_records):
    recs = read_records()
    assert len(recs) == 1, recs
    return recs[0]


def steps(record):
    return [(d["step"], d["outcome"]) for d in record["decisions"]]


# ---- request id, envelope, schema ------------------------------------------------------

def test_every_response_has_a_request_id_matching_exactly_one_record(client, read_records):
    resp = client.get("/view_all_available")
    rid = resp.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", rid)
    assert [r["request_id"] for r in read_records()] == [rid]


def test_request_ids_are_unique(client, read_records):
    ids = {client.get("/view_all_available").headers["X-Request-ID"] for _ in range(20)}
    assert len(ids) == 20 and {r["request_id"] for r in read_records()} == ids


@pytest.mark.parametrize("supplied", ["agent-probe-1", "a", "A.b_c-9", "x" * 64])
def test_valid_client_request_id_is_honoured(client, read_records, supplied):
    resp = client.get("/view_all_available", headers={"X-Request-ID": supplied})
    assert resp.headers["X-Request-ID"] == supplied
    assert only_record(read_records)["request_id"] == supplied


@pytest.mark.parametrize("supplied", ["", "x" * 65, "has space", 'q"uote', "a/b", "{}", "ünï", "a;b"])
def test_invalid_client_request_id_is_replaced_and_noted(client, read_records, supplied):
    resp = client.get("/view_all_available", headers={"X-Request-ID": supplied})
    rid = resp.headers["X-Request-ID"]
    assert rid != supplied and re.fullmatch(r"[0-9a-f]{32}", rid)
    record = only_record(read_records)
    assert record["request_id"] == rid
    assert ("request_id", "client_value_rejected") in steps(record)


def test_record_schema(client, read_records):
    client.get("/search?lat=0&lng=0&radius=5")
    record = only_record(read_records)
    assert set(record) == TOP_LEVEL_KEYS
    assert record["schema"] == 1
    assert TS_RE.match(record["ts"]), record["ts"]
    assert isinstance(record["duration_ms"], float) and record["duration_ms"] >= 0
    assert record["git_commit"] is None or re.fullmatch(r"[0-9a-f]{40}", record["git_commit"])
    assert record["git_dirty"] in (True, False, None)
    assert record["pid"] == os.getpid()
    for decision in record["decisions"]:
        assert set(decision) == {"step", "outcome", "detail"} and isinstance(decision["detail"], dict)


def test_git_state_is_computed_from_this_repo():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True)
    if head.returncode != 0:
        pytest.skip("git not available")
    assert records.GIT_COMMIT == head.stdout.strip()
    assert isinstance(records.GIT_DIRTY, bool)
    assert records.GIT_DIRTY == bool(records.GIT_DIRTY_PATHS)
    assert "scooter_db.json" not in records.GIT_DIRTY_PATHS


def test_git_state_falls_back_when_git_is_missing(monkeypatch):
    monkeypatch.setattr(records.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    assert records._git_state() == (None, None, None)


def _fake_git(monkeypatch, porcelain):
    def fake(*args):
        return "a" * 40 + "\n" if args[0] == "rev-parse" else porcelain
    monkeypatch.setattr(records, "_git", fake)


def test_data_only_changes_are_not_dirty(monkeypatch):
    # A reservation against the live DB rewrites the tracked scooter_db.json: that's data.
    _fake_git(monkeypatch, " M scooter_db.json\0")
    assert records._git_state() == ("a" * 40, False, [])


def test_code_changes_are_dirty_and_listed(monkeypatch):
    _fake_git(monkeypatch, " M app.py\0 M scooter_db.json\0?? planted.py\0R  new_name.py\0old_name.py\0")
    assert records._git_state() == ("a" * 40, True, ["app.py", "new_name.py", "planted.py"])


def test_dirty_paths_are_capped(monkeypatch):
    _fake_git(monkeypatch, "".join(f"?? f{i:03}.py\0" for i in range(100)))
    commit, dirty, paths = records._git_state()
    assert dirty is True and len(paths) == records.MAX_DIRTY_PATHS == 20 and paths[0] == "f000.py"


def test_clean_tree_is_not_dirty(monkeypatch):
    _fake_git(monkeypatch, "")
    assert records._git_state() == ("a" * 40, False, [])


# ---- per endpoint and outcome ----------------------------------------------------------

def test_start_success_records_the_transition(client, read_records):
    client.get("/reservation/start?id=18")
    r = only_record(read_records)
    assert (r["method"], r["path"], r["params"], r["scooter_id"], r["status"], r["error"]) == \
        ("GET", "/reservation/start", {"id": "18"}, "18", 200, None)
    assert r["scooter_before"] == SEED["18"]
    assert r["scooter_after"] == r["scooter_in_memory"] == reserved_18(r["scooter_after"])
    assert r["scooter_after"]["reservation"]["start_request_id"] == r["request_id"]
    assert r["db_write"] == {"attempted": True, "ok": True, "error": None}
    assert steps(r) == [("db_lock", "acquired"), ("db_read", "ok"), ("lookup", "found"), ("reserve", "allowed"),
                        ("reservation", "created"), ("db_write", "ok")]
    created = [d for d in r["decisions"] if d["step"] == "reservation"][0]
    assert created["detail"] == {"reservation_id": r["scooter_after"]["reservation"]["reservation_id"]}


def test_start_already_reserved_records_no_change(client, read_records):
    client.get("/reservation/start?id=33")
    r = only_record(read_records)
    assert r["scooter_before"] == r["scooter_after"] == r["scooter_in_memory"] == SEED["33"]
    assert r["db_write"] == {"attempted": False, "ok": None, "error": None}
    assert r["error"]["kind"] == "scooter_already_reserved" and r["status"] == 422
    assert steps(r)[-2:] == [("reserve", "refused"), ("respond", "error")]


def test_start_unknown_scooter(client, read_records):
    client.get("/reservation/start?id=nope")
    r = only_record(read_records)
    assert r["scooter_id"] == "nope" and r["scooter_before"] is None and r["scooter_after"] is None
    assert ("lookup", "not_found") in steps(r) and r["error"]["kind"] == "scooter_not_found"


def test_end_success_records_payment_and_move(client, read_records):
    client.get("/reservation/start?id=18")
    client.get("/reservation/end?id=18&lat=23.01&lng=19.01")
    r = read_records()[-1]
    assert r["scooter_before"] == reserved_18(r["scooter_before"])
    assert r["scooter_after"] == r["scooter_in_memory"] == \
        {"id": "18", "lat": 23.01, "lng": 19.01, "is_reserved": False, "reservation": None}
    assert r["db_write"]["ok"] is True and r["status"] == 200
    # two-phase end (PLAN PR 14): persist payment_pending, charge, then persist the end
    assert steps(r) == [("params", "ok"), ("db_lock", "acquired"), ("db_read", "ok"), ("lookup", "found"), ("end", "allowed"),
                        ("payment", "pending"), ("db_write", "ok"), ("payment", "requested"), ("payment", "ok"),
                        ("reservation", "cleared"), ("db_write", "ok")]
    cleared = [d for d in r["decisions"] if d["step"] == "reservation"][0]
    assert cleared["detail"] == {"reservation_id": r["scooter_before"]["reservation"]["reservation_id"], "legacy": False}
    payment = [d for d in r["decisions"] if d["step"] == "payment"]
    key = r["scooter_before"]["reservation"]["idempotency_key"]
    assert payment[0]["detail"] == {"end_lat": 23.01, "end_lng": 19.01, "distance_m": payment[0]["detail"]["amount"],
                                    "amount": payment[0]["detail"]["amount"]}
    assert payment[1]["detail"]["cost"] == payment[1]["detail"]["distance_m"] == payment[0]["detail"]["amount"] > 0
    assert payment[1]["detail"]["idempotency_key"] == key
    assert (payment[1]["detail"]["end_lat"], payment[1]["detail"]["end_lng"]) == (23.01, 19.01)
    detail = payment[2]["detail"]
    assert re.fullmatch(r"[0-9a-f]{32}", detail["txn_id"])
    assert detail == {"gateway_outcome": "success", "txn_id": detail["txn_id"], "attempt": 1,
                      "idempotency_key": key, "reservation_id": key,             # what it was charged under
                      "fault": {"source": "env", "mode": "none"},
                      "simulator": {"charged": True, "ledger_txn_id": detail["txn_id"]}}


def test_end_payment_failed_records_ride_left_open(client, monkeypatch, read_records):
    client.get("/reservation/start?id=18")
    monkeypatch.setenv("PAYMENT_FAULT", "error")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    r = read_records()[-1]
    assert ("payment", "failed") in steps(r)
    failed = [d["detail"] for d in r["decisions"] if d["step"] == "payment"][-1]
    assert (failed["gateway_outcome"], failed["txn_id"], failed["fault"]) == ("error", None, {"source": "env", "mode": "error"})
    # the ride is left payment_pending (PLAN PR 14), at the requested location and amount
    assert r["scooter_before"] == reserved_18(r["scooter_before"])
    pending = r["scooter_after"]["reservation"]
    assert r["scooter_after"] == r["scooter_in_memory"] == dict(r["scooter_before"], reservation=pending)
    assert (pending["payment_state"], pending["end_lat"], pending["end_lng"], pending["payment_attempts"],
            pending["last_payment_outcome"]) == ("payment_pending", 1.0, 1.0, 1, "error")
    assert r["db_write"] == {"attempted": True, "ok": True, "error": None}
    assert r["error"]["kind"] == "payment_failed" and r["status"] == 502


def test_end_without_reservation(client, read_records):
    client.get("/reservation/end?id=18&lat=1&lng=1")
    r = only_record(read_records)
    assert ("end", "refused") in steps(r)
    assert r["scooter_before"] == r["scooter_after"] == SEED["18"]
    assert r["error"]["kind"] == "no_active_reservation"


def test_search_records_params_and_matches(client, read_records):
    client.get("/search?lat=0&lng=0&radius=10")
    r = only_record(read_records)
    assert r["params"] == {"lat": "0", "lng": "0", "radius": "10"}
    assert r["decisions"][0] == {"step": "params", "outcome": "ok", "detail": {"lat": 0.0, "lng": 0.0, "radius_m": 10.0}}
    assert r["decisions"][-1] == {"step": "search", "outcome": "ok",
                                  "detail": {"matches": ["14"], "unavailable": {"33": "reserved"}}}


def test_view_all_and_redirect(client, read_records):
    client.get("/")
    client.get("/view_all_available")
    redirect, view = read_records()
    assert (redirect["path"], redirect["status"]) == ("/", 302)
    assert view["decisions"][-1] == {"step": "filter", "outcome": "ok",
                                     "detail": {"available": 4, "listed": ["15", "18", "9", "14"],
                                                "unavailable": {"33": "reserved"}}}


def test_repeated_params_are_kept_as_lists(client, read_records):
    client.get("/search?lat=0&lng=0&radius=-1&radius=5")
    assert only_record(read_records)["params"]["radius"] == ["-1", "5"]


def test_long_values_are_truncated_in_the_record(client, read_records):
    client.get("/reservation/start?id=" + "x" * 5000)
    r = only_record(read_records)
    assert r["params"]["id"].startswith("x" * records.MAX_VALUE_CHARS)
    assert r["params"]["id"].endswith("...(truncated, 5000 chars)")
    assert len(r["scooter_id"]) < 1100


@pytest.mark.parametrize("kind", list(FailureKind), ids=lambda k: k.value)
def test_every_failure_kind_is_recorded(kind, client, monkeypatch, tmp_path, read_records):
    resp = TRIGGERS[kind](client, monkeypatch, tmp_path)
    r = read_records()[-1]
    assert r["request_id"] == resp.headers["X-Request-ID"]
    assert r["status"] == resp.status_code
    assert r["error"] == {"kind": kind.value, "msg": resp.get_json()["msg"]}
    assert r["decisions"][-1] == {"step": "respond", "outcome": "error", "detail": {"kind": kind.value}}


def test_method_not_allowed_records_the_method(client, read_records):
    client.post("/search")
    r = only_record(read_records)
    assert (r["method"], r["status"], r["error"]["kind"]) == ("POST", 405, "method_not_allowed")


def test_internal_error_record_has_exception_details(client, monkeypatch, read_records):
    def boom(distance):
        raise RuntimeError("planted bug")
    monkeypatch.setattr(app_module, "calculate_cost", boom)
    client.get("/reservation/start?id=18")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    r = read_records()[-1]
    assert r["exception"] == {"type": "RuntimeError", "message": "planted bug"}
    assert r["scooter_before"] == reserved_18(r["scooter_before"]) and r["scooter_after"] is None
    assert ("payment", "requested") not in steps(r)        # it crashed while computing the cost


def test_storage_read_failure_is_recorded(client, monkeypatch, tmp_path, read_records):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("SCOOTER_DB_PATH", str(missing))
    client.get("/view_all_available")
    r = only_record(read_records)
    assert r["decisions"][0] == {"step": "db_read", "outcome": "failed",
                                 "detail": {"db_path": str(missing), "error": "FileNotFoundError"}}
    assert r["exception"]["type"] == "FileNotFoundError" and r["error"]["kind"] == "storage_error"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_storage_write_failure_is_recorded(client, db_path, read_records):
    db_path.chmod(0o444)
    try:
        client.get("/reservation/start?id=18")
    finally:
        db_path.chmod(0o644)
    r = only_record(read_records)
    assert r["db_write"] == {"attempted": True, "ok": False, "error": "PermissionError"}
    assert ("db_write", "failed") in steps(r)
    assert r["scooter_before"] == SEED["18"] and r["scooter_after"] is None
    assert r["error"]["kind"] == "storage_error"


# ---- robustness ----------------------------------------------------------------------

def test_records_directory_is_created_on_demand(client, monkeypatch, tmp_path):
    path = tmp_path / "a" / "b" / "records.jsonl"
    monkeypatch.setenv("SCOOTER_RECORDS_PATH", str(path))
    client.get("/view_all_available")
    assert len(path.read_text().splitlines()) == 1


def test_unwritable_records_never_break_the_response(client, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SCOOTER_RECORDS_PATH", str(tmp_path))     # a directory: open() fails
    resp = client.get("/view_all_available")
    assert resp.status_code == 200 and len(resp.get_json()) == 4
    assert resp.headers["X-Record-Error"] == "IsADirectoryError"
    assert "REQUEST RECORD NOT WRITTEN" in caplog.text and resp.headers["X-Request-ID"] in caplog.text


def test_successful_record_has_no_record_error_header(client):
    assert "X-Record-Error" not in client.get("/view_all_available").headers


def test_teardown_writes_a_record_when_no_response_was_produced(read_records):
    # Safety net: if after_request never runs, teardown still writes exactly one record.
    flask_app = app_module.app
    with flask_app.test_request_context("/view_all_available?id=7"):
        flask_app.preprocess_request()                        # before_request: record begins
        flask_app.do_teardown_request(RuntimeError("lost"))   # no response, straight to teardown
    r = only_record(read_records)
    assert r["status"] == 500 and r["exception"] == {"type": "RuntimeError", "message": "lost"}
    assert r["error"]["kind"] == "internal_error"


# ---- concurrency and persistence --------------------------------------------------------

def test_concurrent_requests_from_threads_never_interleave(records_path, read_records):
    def worker():
        client = app_module.app.test_client()
        for _ in range(25):
            assert client.get("/view_all_available").status_code == 200
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = records_path.read_text().splitlines()
    assert len(lines) == 200
    assert len({json.loads(line)["request_id"] for line in lines}) == 200


_WRITER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import records
path, tag, n, size = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
for i in range(n):
    records.append_line(path, json.dumps({"tag": tag, "i": i, "pad": tag * size}))
"""


def test_concurrent_processes_never_interleave_large_lines(tmp_path):
    # Lines far larger than PIPE_BUF (4 KiB) from 4 processes at once: every line must
    # still parse and be complete.
    path = tmp_path / "multi.jsonl"
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(REPO), str(path), tag, "150", "70000"])
             for tag in "ABCD"]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    lines = path.read_text().splitlines()
    assert len(lines) == 600
    seen = {}
    for line in lines:
        rec = json.loads(line)
        assert rec["pad"] == rec["tag"] * 70000
        seen.setdefault(rec["tag"], []).append(rec["i"])
    assert {t: sorted(v) for t, v in seen.items()} == {t: list(range(150)) for t in "ABCD"}


_RESTART = """
import sys
sys.path.insert(0, sys.argv[1])
import app
resp = app.app.test_client().get("/reservation/start?id=18")
print(resp.headers["X-Request-ID"])
"""


def test_records_survive_a_restart(client, db_path, records_path, read_records):
    # First "server": a separate process. Second: this one. Same records file and DB.
    env = dict(os.environ, SCOOTER_DB_PATH=str(db_path), SCOOTER_RECORDS_PATH=str(records_path))
    out = subprocess.run([sys.executable, "-c", _RESTART, str(REPO)], env=env,
                         capture_output=True, text=True, check=True)
    first_id = out.stdout.strip()
    second_id = client.get("/reservation/start?id=18").headers["X-Request-ID"]
    first, second = read_records()
    assert (first["request_id"], second["request_id"]) == (first_id, second_id)
    assert first["pid"] != second["pid"]
    assert first["scooter_after"]["is_reserved"] is True              # persisted by process 1
    assert second["error"]["kind"] == "scooter_already_reserved"      # seen by process 2


def test_append_waits_for_the_exclusive_lock(tmp_path):
    # O_APPEND alone usually keeps lines whole, so the concurrency tests can't prove the
    # lock is taken. This one can: while another holder has the flock, append must wait.
    import fcntl
    path = tmp_path / "locked.jsonl"
    path.write_text("")
    done = threading.Event()
    with open(path, "a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        writer = threading.Thread(target=lambda: (records.append_line(str(path), '{"x": 1}'), done.set()))
        writer.start()
        assert not done.wait(0.5), "append_line wrote without taking the lock"
        assert path.read_text() == ""
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    assert done.wait(5)
    writer.join()
    assert path.read_text() == '{"x": 1}\n'


# ---- PR #11 review: honest after-state, params cap, line safety ----------------------------

_PLANT = """
import json, sys
sys.path.insert(0, sys.argv[1])
import app
resp = app.app.test_client().get("/reservation/end?id=18&lat=23.01&lng=19.01")
print(resp.status_code)
"""
_CORRECT_ORDER = """\t\t\t\t\tfinish_ride(scooter)
\t\t\t\t\twrite_db(db, txn.path)
"""
_BUGGY_ORDER = """\t\t\t\t\twrite_db(db, txn.path)
\t\t\t\t\tfinish_ride(scooter)
"""


def test_planted_write_ordering_bug_is_visible_in_the_record(tmp_path, db_path, records_path, read_db, read_records):
    # The reviewer's planted bug: end_reservation writes the DB *before* changing the
    # scooter. The response still says 200 "ended", but the file keeps it reserved. The
    # record must show what was written (scooter_after) next to what the code believed
    # (scooter_in_memory), so the discrepancy is in the evidence itself.
    src = (REPO / "app.py").read_text()
    assert src.count(_CORRECT_ORDER) == 1, "end_reservation changed shape; update this test"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "app.py").write_text(src.replace(_CORRECT_ORDER, _BUGGY_ORDER))
    for name in ("records.py", "failures.py", "payments.py"):
        (planted / name).write_text((REPO / name).read_text())
    data = json.loads(db_path.read_text())
    next(s for s in data if s["id"] == "18")["is_reserved"] = True
    db_path.write_text(json.dumps(data))
    env = dict(os.environ, SCOOTER_DB_PATH=str(db_path), SCOOTER_RECORDS_PATH=str(records_path),
               PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run([sys.executable, "-c", _PLANT, str(planted)], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "200"                                   # the bug "succeeds"
    on_disk = read_db()["18"]
    # 18 was set reserved in a legacy-shaped seed, so it carries the synthesised legacy
    # reservation; the buggy second write saved it still payment_pending (phase 1's state)
    pending = on_disk["reservation"]
    assert {k: pending[k] for k in LEGACY_33} == dict(LEGACY_33, reservation_id="legacy-18", idempotency_key="legacy-18",
                                                      payment_state="payment_pending")
    assert (pending["end_lat"], pending["end_lng"]) == (23.01, 19.01)
    assert {k: on_disk[k] for k in ("id", "lat", "lng", "is_reserved")} == {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": True}
    r = only_record(read_records)
    assert r["db_write"]["ok"] is True
    assert r["scooter_after"] == on_disk                                  # the truth on disk
    assert r["scooter_in_memory"] == {"id": "18", "lat": 23.01, "lng": 19.01, "is_reserved": False, "reservation": None}
    assert r["scooter_after"] != r["scooter_in_memory"]                   # the discrepancy


def test_after_state_comes_from_the_serialised_data(client, monkeypatch, read_db, read_records):
    # Same idea in-process: whatever write_db actually serialises is what scooter_after shows.
    real = app_module.convert_db_to_dictlist

    def stale(db):
        rows = real(db)
        for row in rows:
            if row["id"] == "18":
                row.update(is_reserved=False, reservation=None)         # pretend the change was lost
        return rows
    monkeypatch.setattr(app_module, "convert_db_to_dictlist", stale)
    client.get("/reservation/start?id=18")
    r = only_record(read_records)
    assert r["scooter_after"] == read_db()["18"] == SEED["18"]
    assert r["scooter_in_memory"] == reserved_18(r["scooter_in_memory"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_end_with_unwritable_db_has_no_after_state(client, db_path, read_records):
    client.get("/reservation/start?id=18")
    db_path.chmod(0o444)
    try:
        client.get("/reservation/end?id=18&lat=1&lng=1")
    finally:
        db_path.chmod(0o644)
    r = read_records()[-1]
    assert r["db_write"] == {"attempted": True, "ok": False, "error": "PermissionError"}
    assert r["scooter_after"] is None                       # nothing known to be persisted
    assert r["scooter_in_memory"] is None                   # the route never got past write_db
    assert r["scooter_before"] == reserved_18(r["scooter_before"])


def test_param_count_is_capped(client, read_records):
    query = "&".join(f"k{i}=v" for i in range(2000))
    client.get("/view_all_available?" + query)
    r = only_record(read_records)
    assert r["params_truncated"] is True and len(r["params"]) == records.MAX_PARAMS == 50
    assert list(r["params"])[:2] == ["k0", "k1"]


def test_params_under_the_cap_are_not_marked_truncated(client, read_records):
    client.get("/view_all_available?" + "&".join(f"k{i}=v" for i in range(50)))
    r = only_record(read_records)
    assert r["params_truncated"] is False and len(r["params"]) == 50


@pytest.mark.parametrize("char", ["\u2028", "\u2029", "\u0085", "\x1c", "\x0b", "\r"])
def test_unicode_line_breaks_stay_on_one_ascii_line(client, records_path, read_records, char):
    from urllib.parse import quote
    client.get("/reservation/start?id=" + quote("a" + char + "b"))
    raw = records_path.read_bytes()
    assert raw.isascii(), "records must be written with ensure_ascii=True"
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert raw.decode().splitlines() == [raw.decode().rstrip("\n")]  # even Python's splitlines agrees
    assert only_record(read_records)["scooter_id"] == "a" + char + "b"

