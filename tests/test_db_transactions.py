"""Transactional DB access (PLAN PR 11): one lock per read-check-write, atomic replace."""
import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import app as app_module
import records_query as rq

REPO = Path(__file__).resolve().parent.parent
SEED_IDS = ["15", "18", "9", "14", "33"]


def lock_path(db_path):
    return Path(app_module.lock_path_for(str(db_path)))


# ---- races inside one process (threads) ------------------------------------------------

@pytest.mark.parametrize("round_", range(10))
def test_simultaneous_reservations_from_threads_exactly_one_wins(db_path, read_db, round_):
    barrier = threading.Barrier(8)
    results = []

    def worker():
        client = app_module.app.test_client()
        barrier.wait()
        results.append(client.get("/reservation/start?id=18").status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200] + [422] * 7
    assert read_db()["18"]["is_reserved"] is True


def test_concurrent_writers_on_different_scooters_lose_no_updates(db_path, read_db, read_records):
    # Every write rewrites the whole file: without the lock, one thread's write can
    # overwrite another's change to a *different* scooter (a lost update).
    def cycle(sid, n):
        client = app_module.app.test_client()
        for i in range(n):
            assert client.get(f"/reservation/start?id={sid}").status_code == 200
            assert client.get(f"/reservation/end?id={sid}&lat={i}&lng={i}").status_code == 200

    threads = [threading.Thread(target=cycle, args=(sid, 15)) for sid in ["15", "18", "9", "14"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db = read_db()
    for sid in ["15", "18", "9", "14"]:
        assert db[sid] == {"id": sid, "lat": 14.0, "lng": 14.0, "is_reserved": False}
    assert db["33"]["is_reserved"] is True
    assert rq.failure_counts(read_records()) == {}


# ---- races across processes --------------------------------------------------------------

_RACER = """
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import app
client = app.app.test_client()
go = float(sys.argv[2])
while time.time() < go:            # start together
    pass
mode = sys.argv[3]
if mode == "race":
    print(client.get("/reservation/start?id=18").status_code)
else:                               # mode == "cycle:<id>:<n>"
    _, sid, n = mode.split(":")
    codes = []
    for i in range(int(n)):
        codes.append(client.get(f"/reservation/start?id={sid}").status_code)
        codes.append(client.get(f"/reservation/end?id={sid}&lat={i}&lng={i}").status_code)
    print(json.dumps(codes))
"""


def _spawn(db_path, records_path, modes, extra_env=None):
    env = dict(os.environ, SCOOTER_DB_PATH=str(db_path), SCOOTER_RECORDS_PATH=str(records_path),
               PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
    go = str(time.time() + 1.5)
    procs = [subprocess.Popen([sys.executable, "-c", _RACER, str(REPO), go, mode], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for mode in modes]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [err for _, err in outs]
    return [out.strip() for out, _ in outs]


@pytest.mark.parametrize("round_", range(3))
def test_simultaneous_reservations_from_processes_exactly_one_wins(db_path, records_path, read_db, round_):
    codes = sorted(int(c) for c in _spawn(db_path, records_path, ["race"] * 6))
    assert codes == [200] + [422] * 5
    assert read_db()["18"]["is_reserved"] is True


def test_concurrent_cycles_across_processes(db_path, records_path, read_db, read_records):
    # 4 processes each cycle their own scooter, plus 4 that fight over scooter 18.
    own = [f"cycle:{sid}:10" for sid in ["15", "9", "14"]]
    shared = ["cycle:18:10"] * 4
    outputs = _spawn(db_path, records_path, own + shared)
    db = read_db()
    for sid in ["15", "9", "14"]:                               # no lost updates
        assert db[sid] == {"id": sid, "lat": 9.0, "lng": 9.0, "is_reserved": False}
    records = read_records()
    assert len(records) == 7 * 20
    assert rq.failure_counts(records).get("storage_error", 0) == 0
    assert rq.failure_counts(records).get("storage_busy", 0) == 0
    assert rq.anomalies(records) == []
    # Scooter 18: successful starts and ends must alternate, and the final state must agree.
    hist = [r for r in records if r["scooter_id"] == "18" and r["status"] == 200]
    starts = sum(r["path"] == "/reservation/start" for r in hist)
    ends = sum(r["path"] == "/reservation/end" for r in hist)
    assert starts - ends == (1 if db["18"]["is_reserved"] else 0)
    # Every successful request's persisted after-state chains: replay them in lock order.
    by_lock = sorted((r for r in records if r["scooter_id"] == "18" and r["db_write"]["ok"]),
                     key=lambda r: r["ts"])
    assert [r["scooter_after"]["is_reserved"] for r in by_lock][-1] == db["18"]["is_reserved"]
    assert all(json.loads(o) for o in outputs)


# ---- atomic writes -------------------------------------------------------------------------

def _no_temp_files(db_path):
    return [p.name for p in db_path.parent.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("exc", [OSError("disk full"), RuntimeError("crash")])
def test_crash_mid_write_leaves_the_old_db_intact(client, monkeypatch, db_path, exc):
    before = db_path.read_bytes()

    def crash(src, dst):
        raise exc                                        # after the temp write, before the replace
    monkeypatch.setattr(app_module.os, "replace", crash)
    resp = client.get("/reservation/start?id=18")
    assert resp.status_code == 500
    assert resp.get_json()["error"]["kind"] == ("storage_error" if isinstance(exc, OSError) else "internal_error")
    assert db_path.read_bytes() == before
    assert _no_temp_files(db_path) == []


def test_write_replaces_the_db_inode_but_not_the_lock(client, db_path):
    client.get("/view_all_available")
    db_inode = db_path.stat().st_ino
    client.get("/reservation/start?id=18")
    lock_inode = lock_path(db_path).stat().st_ino
    assert db_path.stat().st_ino != db_inode                 # os.replace: a new file
    client.get("/reservation/end?id=18&lat=1&lng=1")
    assert lock_path(db_path).stat().st_ino == lock_inode    # the lock file is stable
    assert _no_temp_files(db_path) == []


def test_write_keeps_the_db_permissions(client, db_path):
    db_path.chmod(0o640)
    client.get("/reservation/start?id=18")
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o640


def test_written_file_is_complete_json(client, db_path):
    client.get("/reservation/start?id=18")
    assert [s["id"] for s in json.loads(db_path.read_text())] == SEED_IDS


def test_readers_never_see_a_torn_file(db_path, read_records):
    # A big DB makes each write slow; a reader racing many writes must always get a whole
    # file. Without the atomic replace a reader can read a truncated file -> storage_error.
    big = [{"id": str(i), "lat": 1.0, "lng": 1.0, "is_reserved": False} for i in range(20000)]
    db_path.write_text(json.dumps(big))
    stop = threading.Event()
    codes = []

    def writer():
        client = app_module.app.test_client()
        while not stop.is_set():
            client.get("/reservation/start?id=7")
            client.get("/reservation/end?id=7&lat=1&lng=1")

    def reader():
        client = app_module.app.test_client()
        for _ in range(40):
            codes.append(client.get("/view_all_available").status_code)

    w = threading.Thread(target=writer)
    w.start()
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for r in readers:
        r.start()
    for r in readers:
        r.join()
    stop.set()
    w.join()
    assert codes == [200] * 120
    assert rq.failure_counts(read_records()) == {}


# ---- lock timeout ------------------------------------------------------------------------

def test_lock_timeout_is_storage_busy_and_changes_nothing(client, monkeypatch, db_path, read_records):
    monkeypatch.setenv("SCOOTER_DB_LOCK_TIMEOUT", "0.2")
    before = db_path.read_bytes()
    with open(lock_path(db_path), "a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        resp = client.get("/reservation/start?id=18")
        elapsed = time.monotonic() - started
        view = client.get("/view_all_available")              # readers don't need the lock
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "1"
    assert resp.get_json() == {"msg": "Error 503 - The scooter database is busy; nothing was changed. Retry shortly.",
                               "error": {"kind": "storage_busy"}}
    assert 0.2 <= elapsed < 2
    assert view.status_code == 200
    assert db_path.read_bytes() == before
    record = read_records()[0]
    lock_step = next(d for d in record["decisions"] if d["step"] == "db_lock")
    assert lock_step["outcome"] == "timeout" and lock_step["detail"]["wait_ms"] >= 200
    assert record["db_write"]["attempted"] is False


def test_lock_is_released_after_every_request(client, db_path):
    client.get("/reservation/start?id=18")
    client.get("/reservation/start?id=18")                   # a refused request releases it too
    client.get("/reservation/start?id=nope")
    with open(lock_path(db_path), "a") as probe:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)   # would raise if still held


def test_lock_is_released_when_the_request_crashes(client, monkeypatch, db_path):
    monkeypatch.setattr(app_module, "calculate_cost", lambda d: 1 / 0)
    client.get("/reservation/start?id=18")
    assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == 500
    with open(lock_path(db_path), "a") as probe:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_lock_wait_is_recorded(client, read_records):
    client.get("/reservation/start?id=18")
    step = next(d for d in read_records()[0]["decisions"] if d["step"] == "db_lock")
    assert step["outcome"] == "acquired" and step["detail"]["wait_ms"] >= 0
    assert step["detail"]["lock_path"].endswith("scooter_db.json.lock")


@pytest.mark.parametrize("value,expected", [("", 5.0), ("0.5", 0.5), ("x", 5.0), ("-3", 0.0)])
def test_lock_timeout_setting(monkeypatch, value, expected):
    monkeypatch.setenv("SCOOTER_DB_LOCK_TIMEOUT", value)
    assert app_module.get_db_lock_timeout() == expected


def test_reads_take_no_lock(client, db_path, read_records):
    client.get("/view_all_available")
    client.get("/search?lat=0&lng=0&radius=1")
    assert all(d["step"] != "db_lock" for r in read_records() for d in r["decisions"])


def test_write_fsyncs_the_temp_file_and_the_directory(client, monkeypatch, db_path):
    # Durability against power loss can't be tested by crashing the machine, so check the
    # mechanism: the temp file's data and the directory entry (the rename) are both fsynced,
    # in that order, around the replace.
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        events.append("fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file")
        return real_fsync(fd)

    def spy_replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)
    monkeypatch.setattr(app_module.os, "fsync", spy_fsync)
    monkeypatch.setattr(app_module.os, "replace", spy_replace)
    client.get("/reservation/start?id=18")
    assert events == ["fsync_file", "replace", "fsync_dir"]
