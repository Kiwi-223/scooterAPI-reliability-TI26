"""All-or-nothing end-ride (PLAN PR 14; ACTIVITY Outcome 3): the payment state machine.

active --end--> payment_pending (persisted, with end location + amount) --charge--> one of:
  paid / already_charged -> ended (free at the stored end location)
  decline                -> ended + outstanding balance (H3), 422 with ride_ended: true (H7)
  timeouts / error       -> still payment_pending (504 / 502); a retry or reconcile settles it
  conflict               -> back to active (409); nothing charged

The invariants (tests/invariants.py) are checked against the DB, the ledger and the records
after every step of every fault x scenario, and after a seeded random soak.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app as app_module
import payments
import records_query as rq
from failures import FailureKind
from invariants import started_keys, violations
from test_failure_kinds import assert_envelope

REPO = Path(__file__).resolve().parent.parent
FAULTS = ["none", "decline", "timeout_not_charged", "timeout_charged", "error"]
FIRST_STATUS = {"none": 200, "decline": 422, "timeout_not_charged": 504, "timeout_charged": 504, "error": 502}


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    monkeypatch.setenv("PAYMENT_TIMEOUT_DELAY_MS", "0")


def raw_db(db_path):
    return json.loads(db_path.read_text())


def by_id(db_path):
    return {s["id"]: s for s in raw_db(db_path)}


def start_18(client):
    resp = client.get("/reservation/start?id=18")
    assert resp.status_code == 200
    return resp.get_json()["reservation_id"]


@pytest.fixture
def assert_invariants(db_path, read_ledger, read_records):
    initial = raw_db(db_path)

    def check():
        found = violations(raw_db(db_path), read_ledger(), started_keys(read_records(), initial))
        assert found == [], found
    return check


# ---- phase 1 happens before the charge --------------------------------------------------------

def test_payment_pending_is_on_disk_before_the_gateway_is_called(client, monkeypatch, db_path):
    real = payments.charge
    seen = []

    def spy(*args, **kwargs):
        seen.append(by_id(db_path)["18"])                         # what's persisted at charge time
        return real(*args, **kwargs)
    monkeypatch.setattr(payments, "charge", spy)
    key = start_18(client)
    assert client.get("/reservation/end?id=18&lat=1.5&lng=2.5").status_code == 200
    [on_disk] = seen
    reservation = on_disk["reservation"]
    assert on_disk["is_reserved"] is True and (on_disk["lat"], on_disk["lng"]) == (23.0, 19.0)
    assert (reservation["payment_state"], reservation["idempotency_key"]) == ("payment_pending", key)
    assert (reservation["end_lat"], reservation["end_lng"], reservation["payment_attempts"]) == (1.5, 2.5, 0)
    assert reservation["amount"] == reservation["distance_m"] > 0


def test_no_charge_when_the_ride_cannot_be_ended(client, db_path, read_db, read_ledger, read_records):
    # Was a strict xfail (PLAN PR 14): with an unwritable DB the gateway was charged first and
    # the save failed after. Now phase 1 (payment_pending) must be saved before any charge.
    if os.geteuid() == 0:
        pytest.skip("root can write a read-only file")
    assert client.get("/reservation/start?id=18").status_code == 200
    before = read_db()
    db_path.chmod(0o444)
    try:
        resp = client.get("/reservation/end?id=18&lat=1&lng=1")
    finally:
        db_path.chmod(0o644)
    assert resp.status_code == 500 and resp.get_json()["error"]["kind"] == "storage_error"
    assert read_db() == before and before["18"]["reservation"]["payment_state"] == "active"
    assert read_ledger() == []                                   # the gateway was never called
    r = read_records()[-1]
    assert r["db_write"] == {"attempted": True, "ok": False, "error": "PermissionError"}
    assert not any(d["step"] == "payment" and d["outcome"] == "requested" for d in r["decisions"])


def _fail_the_second_write(monkeypatch):
    """The disk fails on phase 2's write (after the gateway call), the way a real one would:
    write_db sees an OSError, records it and answers storage_error."""
    real = app_module._replace_atomically
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(path, data)
    monkeypatch.setattr(app_module, "_replace_atomically", flaky)
    return lambda: monkeypatch.setattr(app_module, "_replace_atomically", real)


def test_a_charge_whose_final_write_fails_stays_settleable(client, monkeypatch, db_path, read_ledger, records_path,
                                                         assert_invariants):
    key = start_18(client)
    restore = _fail_the_second_write(monkeypatch)
    assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.STORAGE_ERROR)
    restore()
    [charge] = read_ledger()
    assert charge["charged"] and charge["idempotency_key"] == key
    reservation = by_id(db_path)["18"]["reservation"]
    assert reservation["payment_state"] == "payment_pending"     # charged, recoverable, not lost
    assert_invariants()
    found = rq.anomalies(rq.read_records(records_path))
    assert [a["type"] for a in found] == ["charged_without_persisted_end"]
    # a retry (from anywhere) settles it with that charge
    body = client.get("/reservation/end?id=18&lat=8&lng=8").get_json()
    assert body["txn_id"] == charge["txn_id"]
    assert (by_id(db_path)["18"]["lat"], by_id(db_path)["18"]["reservation"]) == (1.0, None)
    assert sum(e["charged"] for e in read_ledger()) == 1
    assert_invariants()


# ---- each outcome ----------------------------------------------------------------------------

def test_decline_ends_the_ride_with_an_outstanding_balance(client, monkeypatch, db_path, read_ledger, assert_invariants):
    key = start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", "decline")
    body = assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.PAYMENT_DECLINED)
    after = by_id(db_path)["18"]
    [balance] = after["outstanding_balances"]
    assert body["ride_ended"] is True and body["outstanding_balance"] == balance["amount"] > 0
    assert (after["is_reserved"], after["reservation"], after["lat"], after["lng"]) == (False, None, 1.0, 1.0)
    assert (balance["reservation_id"], balance["idempotency_key"], balance["reason"]) == (key, key, "payment_declined")
    assert (balance["end_lat"], balance["end_lng"]) == (1.0, 1.0)
    assert not any(e["charged"] for e in read_ledger())
    assert_invariants()
    # the scooter is free again; a second declined ride adds a second balance, keeps the first
    key2 = start_18(client)
    client.get("/reservation/end?id=18&lat=2&lng=2")
    assert [b["reservation_id"] for b in by_id(db_path)["18"]["outstanding_balances"]] == [key, key2]
    assert client.get("/view_all_available").get_json()[1] == {"id": "18", "lat": 2.0, "lng": 2.0, "is_reserved": False}
    assert_invariants()


def test_a_legacy_reservation_can_be_declined(client, monkeypatch, db_path, assert_invariants):
    monkeypatch.setenv("PAYMENT_FAULT", "decline")
    assert client.get("/reservation/end?id=33&lat=24.5&lng=-28").status_code == 422
    assert by_id(db_path)["33"]["outstanding_balances"][0]["idempotency_key"] == "legacy-33"
    assert_invariants()


@pytest.mark.parametrize("fault", ["timeout_not_charged", "error"])
def test_timeouts_and_errors_stay_pending_and_count_attempts(client, monkeypatch, db_path, fault):
    start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", fault)
    for attempt in (1, 2):
        body = client.get("/reservation/end?id=18&lat=1&lng=1").get_json()
        assert body["ride_ended"] is False
        reservation = by_id(db_path)["18"]["reservation"]
        assert (reservation["payment_state"], reservation["payment_attempts"]) == ("payment_pending", attempt)
        assert reservation["last_payment_outcome"] == fault


def test_a_charged_timeout_is_pending_once_then_settles_on_the_next_attempt(client, monkeypatch, db_path, read_ledger):
    start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_charged")             # still forced on the retry
    assert client.get("/reservation/end?id=18&lat=1&lng=1").get_json()["ride_ended"] is False
    assert by_id(db_path)["18"]["reservation"]["last_payment_outcome"] == "timeout_charged"
    body = client.get("/reservation/end?id=18&lat=1&lng=1").get_json()
    assert body["txn_id"] == read_ledger()[0]["txn_id"] and by_id(db_path)["18"]["reservation"] is None


def test_a_retry_ends_the_ride_where_it_was_first_asked_to(client, monkeypatch, db_path, read_records):
    start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_not_charged")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    assert client.get("/reservation/end?id=18&lat=5&lng=5").status_code == 200
    assert (by_id(db_path)["18"]["lat"], by_id(db_path)["18"]["lng"]) == (1.0, 1.0)
    retry = [d for d in read_records()[-1]["decisions"] if d["step"] == "end"]
    assert retry[0]["outcome"] == "retry"
    assert {k: retry[0]["detail"][k] for k in ("end_lat", "end_lng", "requested_lat", "requested_lng")} == \
        {"end_lat": 1.0, "end_lng": 1.0, "requested_lat": 5.0, "requested_lng": 5.0}


def test_a_scooter_waiting_for_payment_cannot_be_reserved_or_found(client, monkeypatch, get_json):
    start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", "error")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    assert_envelope(client.get("/reservation/start?id=18"), FailureKind.SCOOTER_PAYMENT_PENDING)
    assert "18" not in [s["id"] for s in get_json("/view_all_available", 200)]
    assert get_json("/search?lat=1&lng=1&radius=100", 200) == []
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    assert client.get("/reservation/start?id=18").status_code == 200


def test_a_conflict_reopens_the_ride_and_charges_nothing(client, db_path, read_ledger, read_records,
                                                       assert_invariants):
    key = start_18(client)
    snapshot = db_path.read_bytes()
    first = client.get("/reservation/end?id=18&lat=1&lng=1").get_json()["txn_id"]
    db_path.write_bytes(snapshot)                                # a restored DB
    # The restore itself breaks I1: the DB says "riding" for a ride the ledger says was paid.
    i1 = [("I1 charged, but the ride is active (not ended, not pending)", key, None)]
    assert violations(raw_db(db_path), read_ledger(), started_keys(read_records(), [])) == i1
    body = assert_envelope(client.get("/reservation/end?id=18&lat=2&lng=2"), FailureKind.PAYMENT_CONFLICT)
    assert body["ride_ended"] is False
    reservation = by_id(db_path)["18"]["reservation"]
    assert reservation["payment_state"] == "active" and not app_module.PENDING_FIELDS & set(reservation)
    assert reservation["idempotency_key"] == key and sum(e["charged"] for e in read_ledger()) == 1
    # the conflict surfaces it and doesn't make it worse (no charge, no free end)
    assert violations(raw_db(db_path), read_ledger(), started_keys(read_records(), [])) == i1
    # ending it where it really ended settles it with the original charge
    assert client.get("/reservation/end?id=18&lat=1&lng=1").get_json()["txn_id"] == first
    assert_invariants()


# ---- DB validation for the new fields ----------------------------------------------------------

def _pending(**changes):
    reservation = {"reservation_id": "a" * 32, "started_at": None, "start_request_id": None,
                   "payment_state": "payment_pending", "idempotency_key": "a" * 32, "end_lat": 1.0, "end_lng": 2.0,
                   "distance_m": 10, "amount": 10, "payment_requested_at": "2026-11-10T00:00:00.000Z",
                   "payment_request_id": "r", "payment_attempts": 0, "last_payment_outcome": None}
    for key, value in changes.items():
        if value is ...:
            reservation.pop(key)
        else:
            reservation[key] = value
    return reservation


BALANCE = {"reservation_id": "b", "idempotency_key": "b", "amount": 5, "end_lat": 1.0, "end_lng": 1.0,
           "ended_at": "2026-11-10T00:00:00.000Z", "reason": "payment_declined"}


def _db(**entry_33):
    data = json.loads((REPO / "tests" / "seed_db.json").read_text())
    data[4].update(entry_33)
    return data


@pytest.mark.parametrize("data,why", [
    (_db(reservation=_pending(amount=...)), "reservation is payment_pending but missing ['amount']"),
    (_db(reservation=_pending(end_lat=91)), "reservation.end_lat is not a number in [-90, 90]"),
    (_db(reservation=_pending(end_lng="x")), "reservation.end_lng is not a number in [-180, 180]"),
    (_db(reservation=_pending(amount=-1)), "reservation.amount is not a finite number >= 0"),
    (_db(reservation=_pending(payment_attempts=True)), "reservation.payment_attempts is not a whole number >= 0"),
    (_db(reservation=_pending(last_payment_outcome=3)), "reservation.last_payment_outcome is not a string or null"),
    (_db(reservation=dict(_pending(), payment_state="active")), "reservation is active but has payment fields"),
    (_db(outstanding_balances={}), "outstanding_balances is dict, not a list"),
    (_db(outstanding_balances=[dict(BALANCE, amount=None)]), "outstanding_balances[0].amount is not a finite number >= 0"),
    (_db(outstanding_balances=[{k: v for k, v in BALANCE.items() if k != "reason"}]), "outstanding_balances[0] is missing ['reason']"),
])
def test_invalid_payment_fields_are_storage_error(client, db_path, data, why):
    db_path.write_text(json.dumps(data))
    body = assert_envelope(client.get("/view_all_available"), FailureKind.STORAGE_ERROR)
    assert why in body["msg"]


def test_valid_payment_fields_load_and_round_trip(client, db_path):
    data = _db(reservation=_pending(), outstanding_balances=[BALANCE])
    db_path.write_text(json.dumps(data))
    assert client.get("/view_all_available").status_code == 200
    assert client.get("/reservation/start?id=18").status_code == 200    # a write keeps them
    assert by_id(db_path)["33"] == data[4]


# ---- every fault x every scenario: the invariants hold, and every ride settles ---------------

_RETRY = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import app
sync_dir, me = sys.argv[2], sys.argv[3]
if sync_dir != "-":
    open(os.path.join(sync_dir, "ready-" + me), "w").close()
    while not os.path.exists(os.path.join(sync_dir, "go")):
        time.sleep(0.0005)
print(app.app.test_client().get("/reservation/end?id=18&lat=9&lng=9").status_code)
"""


def _retry_in_processes(tmp_path, n, extra_env=None):
    """n fresh interpreters (a restarted server) retry the end at once -> their statuses."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
    sync_dir = tmp_path / f"sync-{time.monotonic_ns()}"
    sync_dir.mkdir()
    sync = str(sync_dir) if n > 1 else "-"
    procs = [subprocess.Popen([sys.executable, "-c", _RETRY, str(REPO), sync, str(i)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(n)]
    if n > 1:
        deadline = time.monotonic() + 120
        while len(list(sync_dir.glob("ready-*"))) < n:
            assert time.monotonic() < deadline and all(p.poll() is None for p in procs), \
                [p.communicate() for p in procs if p.poll() is not None]
            time.sleep(0.01)
        (sync_dir / "go").touch()
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [err for _, err in outs]
    return sorted(int(out) for out, _ in outs)


@pytest.mark.parametrize("scenario", ["first", "retry", "retry_after_restart", "concurrent_retries", "final_write_fails"])
@pytest.mark.parametrize("fault", FAULTS)
def test_invariants_hold_for_every_fault_and_scenario(client, monkeypatch, tmp_path, db_path, read_ledger,
                                                      assert_invariants, fault, scenario):
    key = start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", fault)
    if scenario == "final_write_fails":
        restore = _fail_the_second_write(monkeypatch)
        assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == 500
        restore()
        assert by_id(db_path)["18"]["reservation"]["payment_state"] == "payment_pending"
    else:
        assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == FIRST_STATUS[fault]
    assert_invariants()
    open_after_first = by_id(db_path)["18"]["is_reserved"]

    if scenario == "retry":
        client.get("/reservation/end?id=18&lat=1&lng=1")
    elif scenario == "retry_after_restart":
        _retry_in_processes(tmp_path, 1)
    elif scenario == "concurrent_retries":
        monkeypatch.setenv("PAYMENT_FAULT", "none")
        statuses = _retry_in_processes(tmp_path, 4)
        # exactly one retry settles a pending ride; the rest find it already ended
        assert statuses == ([200, 422, 422, 422] if open_after_first else [422] * 4)
    assert_invariants()

    # settle: with the gateway healthy, a retry (or reconcile, PLAN PR 15) always ends the ride
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    for _ in range(3):
        if not by_id(db_path)["18"]["is_reserved"]:
            break
        client.get("/reservation/end?id=18&lat=7&lng=7")
    final = by_id(db_path)["18"]
    assert final["is_reserved"] is False and (final["lat"], final["lng"]) == (1.0, 1.0)   # the first ask
    charges = [e for e in read_ledger() if e["charged"] and e["idempotency_key"] == key]
    balances = [b for b in final.get("outstanding_balances") or [] if b["idempotency_key"] == key]
    assert len(charges) + len(balances) == 1                         # paid once, or owed once
    assert_invariants()


# ---- a seeded random-fault soak across processes ---------------------------------------------

_SOAKER = """
import json, random, sys
sys.path.insert(0, sys.argv[1])
import app
rng = random.Random(int(sys.argv[2]))
client = app.app.test_client()
statuses = []
for _ in range(int(sys.argv[3])):
    sid = rng.choice(["15", "18", "9", "14", "33"])
    if rng.random() < 0.45:
        resp = client.get(f"/reservation/start?id={sid}")
    else:
        resp = client.get(f"/reservation/end?id={sid}&lat={rng.uniform(-80, 80):.4f}&lng={rng.uniform(-170, 170):.4f}")
    statuses.append(resp.status_code)
print(json.dumps(statuses))
"""


def test_a_random_fault_soak_keeps_every_invariant(client, monkeypatch, db_path, read_ledger, read_records,
                                                   records_path, assert_invariants):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PAYMENT_FAULT="random", PAYMENT_SEED="2026",
               PAYMENT_FAULT_RATE="0.5", SCOOTER_DB_LOCK_TIMEOUT="60")
    procs = [subprocess.Popen([sys.executable, "-c", _SOAKER, str(REPO), str(seed), "125"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for seed in range(4)]
    outs = [p.communicate(timeout=300) for p in procs]
    assert all(p.returncode == 0 for p in procs), [err for _, err in outs]
    statuses = [s for out, _ in outs for s in json.loads(out)]
    assert len(statuses) == 500 and set(statuses) <= {200, 422, 502, 504}, sorted(set(statuses))
    assert_invariants()
    # every fault (and the ledger's already_charged) really happened
    outcomes = {e["outcome"] for e in read_ledger()}
    assert {"success", "decline", "timeout_not_charged", "timeout_charged", "error", "already_charged"} <= outcomes
    # settle every pending ride with a healthy gateway
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    for sid, entry in by_id(db_path).items():
        if (entry.get("reservation") or {}).get("payment_state") == "payment_pending":
            assert client.get(f"/reservation/end?id={sid}&lat=0&lng=0").status_code == 200
    assert not any((e.get("reservation") or {}).get("payment_state") == "payment_pending" for e in raw_db(db_path))
    assert_invariants()
    assert rq.anomalies(rq.read_records(records_path)) == []
    assert any(e.get("outstanding_balances") for e in raw_db(db_path))   # H3 declines were recorded


# ---- the checker itself: it must catch each kind of violation, or the tests above prove nothing

def _entry(sid="18", reservation=None, reserved=None, balances=None):
    entry = {"id": sid, "lat": 0, "lng": 0, "is_reserved": reservation is not None if reserved is None else reserved,
             "reservation": reservation}
    if balances is not None:
        entry["outstanding_balances"] = balances
    return entry


def _charge(key, txn="t1"):
    return {"idempotency_key": key, "charged": True, "txn_id": txn}


@pytest.mark.parametrize("db,ledger,keys,expected", [
    ([_entry()], [_charge("k"), _charge("k", "t2")], {"k"}, "I3 more than one charge"),
    ([_entry(reservation={"idempotency_key": "k", "payment_state": "active"})], [_charge("k")], {"k"},
     "I1 charged, but the ride is active (not ended, not pending)"),
    ([_entry(balances=[{"idempotency_key": "k"}])], [_charge("k")], {"k"},
     "I1 charged, and also recorded as an outstanding balance"),
    ([_entry()], [], {"k"}, "I2 ended without a charge or an outstanding balance"),
    ([_entry(balances=[{"idempotency_key": "k"}, {"idempotency_key": "k"}])], [], {"k"},
     "I2 more than one outstanding balance"),
    ([_entry(reserved=True)], [], set(), "I4 is_reserved disagrees with reservation"),
    ([_entry(reservation={"idempotency_key": "k", "payment_state": "refund_requested"})], [], {"k"},
     "I4 reserved without an active/pending reservation"),
])
def test_the_invariant_checker_catches_each_violation(db, ledger, keys, expected):
    assert expected in [v[0] for v in violations(db, ledger, keys)]


@pytest.mark.parametrize("db,ledger,keys", [
    ([_entry()], [_charge("k")], {"k"}),                                                   # ended + paid
    ([_entry(balances=[{"idempotency_key": "k"}])], [], {"k"}),                            # ended + owed
    ([_entry(reservation={"idempotency_key": "k", "payment_state": "payment_pending"})], [_charge("k")], {"k"}),
    ([_entry(reservation={"idempotency_key": "k", "payment_state": "active"})], [], {"k"}),  # riding
    ([{"id": "33", "lat": 0, "lng": 0, "is_reserved": True}], [], {"legacy-33"}),          # legacy shape
])
def test_the_invariant_checker_accepts_every_legitimate_state(db, ledger, keys):
    assert violations(db, ledger, keys) == []
