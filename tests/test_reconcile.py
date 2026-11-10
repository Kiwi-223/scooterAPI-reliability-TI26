"""Reconcile (PLAN PR 15; H3): every stuck ride reaches a terminal state, safely.

Time is injected (`now=`), never slept. The ledger and DB are the test's own (autouse
fixtures), and every run's records go to the test's records file.
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app as app_module
import payments
import reconcile
import records_query as rq
from invariants import pending_amounts, started_keys, violations

REPO = Path(__file__).resolve().parent.parent
T0 = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("PAYMENT_TIMEOUT_DELAY_MS", "0")


def by_id(db_path):
    return {s["id"]: s for s in json.loads(db_path.read_text())}


def start_and_fail(client, monkeypatch, sid="18", fault="error", lat=1, lng=1):
    """Start a ride and end it with a fault -> its key (the ride is payment_pending)."""
    key = client.get(f"/reservation/start?id={sid}").get_json()["reservation_id"]
    monkeypatch.setenv("PAYMENT_FAULT", fault)
    client.get(f"/reservation/end?id={sid}&lat={lat}&lng={lng}")
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    return key


@pytest.fixture
def check_all(db_path, read_ledger, read_records, records_path, ledger_path):
    initial = json.loads(db_path.read_text())

    def check():
        recs = read_records()
        assert violations(json.loads(db_path.read_text()), read_ledger(), started_keys(recs, initial),
                          pending_amounts(recs)) == []
        assert rq.anomalies(rq.read_records(records_path)) == []
        result = rq.ledger_check(read_ledger(), recs, min_age_s=0)
        assert not rq.ledger_problems(result), result
    return check


# ---- decide(): every branch, pure ------------------------------------------------------------

def _pending(**over):
    reservation = {"reservation_id": "k", "idempotency_key": "k", "payment_state": "payment_pending",
                   "end_lat": 1.0, "end_lng": 2.0, "distance_m": 10, "amount": 10, "payment_attempts": 0,
                   "payment_requested_at": "2026-11-10T11:55:00.000Z"}
    reservation.update(over)
    return {"id": "18", "lat": 0.0, "lng": 0.0, "is_reserved": True, "reservation": reservation}


def _charge(**over):
    entry = {"idempotency_key": "k", "charged": True, "txn_id": "t1", "scooter_id": "18", "reservation_id": "k",
             "end_lat": 1.0, "end_lng": 2.0, "amount": 10}
    entry.update(over)
    return entry


@pytest.mark.parametrize("entry,ledger,action", [
    ({"id": "18", "lat": 0, "lng": 0, "is_reserved": False, "reservation": None}, [], "none"),
    (_pending(), [_charge()], "settle_charged"),
    (_pending(), [_charge(end_lat=1.5)], "report"),                         # a charge for another ride
    (_pending(), [_charge(amount=11)], "report"),
    (_pending(), [], "retry"),
    (_pending(payment_attempts=2), [{"idempotency_key": "k", "charged": False}] * 3, "end_unpaid"),  # ledger counts
    (_pending(payment_attempts=3), [], "end_unpaid"),
    (_pending(payment_requested_at="2026-11-10T11:44:59.000Z"), [], "end_unpaid"),       # 15 min + 1 s
    (_pending(payment_requested_at="2026-11-10T11:45:01.000Z"), [], "retry"),            # just under
    (_pending(payment_attempts=5), [_charge()], "settle_charged"),           # a charge beats the limits
    (dict(_pending(), reservation={"reservation_id": "k", "idempotency_key": "k", "payment_state": "active"}), [], "none"),
    (dict(_pending(), reservation={"reservation_id": "k", "idempotency_key": "k", "payment_state": "weird"}), [], "report"),
])
def test_decide(entry, ledger, action):
    assert reconcile.decide(entry, ledger, T0, 3, datetime.timedelta(minutes=15))["action"] == action


def test_decide_on_an_active_ride_that_was_already_charged():
    active = {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": True,
              "reservation": {"reservation_id": "k", "idempotency_key": "k", "payment_state": "active"}}
    _, cost = reconcile._cost_from(23.0, 19.0, 1.0, 1.0)
    charge = _charge(end_lat=1.0, end_lng=1.0, amount=cost)
    d = reconcile.decide(active, [charge], T0)
    assert (d["action"], d["txn_id"], d["end_lat"], d["amount"]) == ("settle_from_ledger", "t1", 1.0, cost)
    assert reconcile.decide(active, [dict(charge, amount=cost + 1)], T0)["action"] == "report"
    assert reconcile.decide(active, [dict(charge, scooter_id="15")], T0)["action"] == "report"
    assert reconcile.decide(active, [dict(charge, charged=False)], T0)["action"] == "none"


# ---- each branch, end to end -----------------------------------------------------------------

def test_a_charged_timeout_is_settled_with_its_charge(client, monkeypatch, db_path, read_ledger, read_records, check_all):
    key = start_and_fail(client, monkeypatch, fault="timeout_charged")
    summary = reconcile.run()
    [result] = summary["results"]
    charge = read_ledger()[0]
    assert (result["action"], result["result"], result["txn_id"]) == ("settle_charged", "ended_paid", charge["txn_id"])
    assert [e["outcome"] for e in read_ledger()] == ["timeout_charged", "already_charged"]  # no new charge
    assert by_id(db_path)["18"]["reservation"] is None and (by_id(db_path)["18"]["lat"], by_id(db_path)["18"]["lng"]) == (1.0, 1.0)
    assert summary["still_pending"] == [] and not reconcile.remaining(summary)
    record = read_records()[-1]
    assert (record["path"], record["status"], record["request_id"]) == ("/reconcile", 200, result["request_id"])
    assert record["request_id"].startswith(f"reconcile-{summary['run_id']}-")
    assert ("reconcile", "settle_charged") in [(d["step"], d["outcome"]) for d in record["decisions"]]
    check_all()


def test_an_uncharged_ride_is_retried_and_paid(client, monkeypatch, db_path, read_ledger, check_all):
    start_and_fail(client, monkeypatch, fault="timeout_not_charged", lat=3, lng=3)
    [result] = reconcile.run()["results"]
    assert (result["action"], result["result"]) == ("retry", "ended_paid")
    assert [e["outcome"] for e in read_ledger()] == ["timeout_not_charged", "success"]
    assert (by_id(db_path)["18"]["lat"], by_id(db_path)["18"]["lng"]) == (3.0, 3.0)   # the stored location
    check_all()


def test_a_decline_during_reconcile_applies_h3(client, monkeypatch, db_path, check_all):
    start_and_fail(client, monkeypatch, fault="error")
    monkeypatch.setenv("PAYMENT_FAULT", "decline")
    [result] = reconcile.run()["results"]
    assert (result["action"], result["result"], result["gateway_outcome"]) == ("retry", "ended_with_balance", "decline")
    [balance] = by_id(db_path)["18"]["outstanding_balances"]
    assert balance["reason"] == "payment_declined" and balance["amount"] == result["outstanding_balance"] > 0
    check_all()


def test_a_failed_attempt_stays_pending_and_counts(client, monkeypatch, db_path, check_all):
    start_and_fail(client, monkeypatch, fault="error")
    monkeypatch.setenv("PAYMENT_FAULT", "error")
    summary = reconcile.run(now=lambda: T0 - datetime.timedelta(days=1))   # well inside the time limit
    [result] = summary["results"]
    assert (result["action"], result["result"], result["status"]) == ("retry", "still_pending", 202)
    assert by_id(db_path)["18"]["reservation"]["payment_attempts"] == 2
    assert summary["still_pending"] == ["18"] and reconcile.remaining(summary)
    check_all()


def test_after_n_attempts_the_ride_ends_unpaid(client, monkeypatch, db_path, read_ledger, check_all):
    start_and_fail(client, monkeypatch, fault="error")                      # attempt 1
    monkeypatch.setenv("PAYMENT_FAULT", "error")
    early = lambda: datetime.datetime.now(datetime.UTC)
    actions = [reconcile.run(now=early)["results"][0]["action"] for _ in range(3)]
    assert actions == ["retry", "retry", "end_unpaid"]                      # attempts 2, 3, then the rule
    assert len(read_ledger()) == 3                                          # the terminal step charges nothing
    entry = by_id(db_path)["18"]
    [balance] = entry["outstanding_balances"]
    assert entry["reservation"] is None and balance["reason"] == "payment_unresolved"
    assert (entry["lat"], entry["lng"]) == (1.0, 1.0)
    check_all()


def test_after_t_minutes_the_ride_ends_unpaid(client, monkeypatch, db_path, read_ledger, check_all):
    start_and_fail(client, monkeypatch, fault="timeout_not_charged")
    started = datetime.datetime.fromisoformat(by_id(db_path)["18"]["reservation"]["payment_requested_at"].replace("Z", "+00:00"))
    almost = reconcile.run(now=lambda: started + datetime.timedelta(minutes=14, seconds=59), dry_run=True)
    assert almost["results"][0]["action"] == "retry"
    [result] = reconcile.run(now=lambda: started + datetime.timedelta(minutes=15))["results"]
    assert (result["action"], result["result"]) == ("end_unpaid", "ended_with_balance")
    assert "15" in result["reason"] and len(read_ledger()) == 1               # no new attempt
    check_all()


def test_the_limits_are_configurable(client, monkeypatch, db_path):
    start_and_fail(client, monkeypatch, fault="error")
    assert reconcile.run(max_attempts=1)["results"][0]["action"] == "end_unpaid"


def test_a_restored_charged_but_active_ride_is_settled_from_the_ledger(client, db_path, read_ledger, read_records, check_all):
    key = client.get("/reservation/start?id=18").get_json()["reservation_id"]
    snapshot = db_path.read_bytes()
    txn = client.get("/reservation/end?id=18&lat=1&lng=1").get_json()["txn_id"]
    db_path.write_bytes(snapshot)                                            # the restore: I1 broken
    summary = reconcile.run()
    [result] = summary["results"]
    assert (result["action"], result["result"], result["txn_id"]) == ("settle_from_ledger", "ended_paid", txn)
    entry = by_id(db_path)["18"]
    assert entry["reservation"] is None and (entry["lat"], entry["lng"]) == (1.0, 1.0)
    assert [e["charged"] for e in read_ledger()] == [True]                   # nothing new
    check_all()                                                              # I1 holds again


def test_a_restored_ride_that_doesnt_fit_its_charge_is_reported_not_guessed(client, db_path, read_ledger, check_all):
    client.get("/reservation/start?id=18")
    snapshot = json.loads(db_path.read_text())
    client.get("/reservation/end?id=18&lat=1&lng=1")
    next(s for s in snapshot if s["id"] == "18").update(lat=5.0, lng=5.0)     # restored, then moved by hand
    db_path.write_text(json.dumps(snapshot))
    before = db_path.read_bytes()
    summary = reconcile.run()
    [result] = summary["results"]
    assert (result["action"], result["status"]) == ("report", 409) and "amount" in result["reason"]
    assert db_path.read_bytes() == before and reconcile.remaining(summary)


def test_a_pending_ride_whose_key_was_charged_for_another_ride_is_reported(client, monkeypatch, db_path, ledger_path):
    key = start_and_fail(client, monkeypatch, fault="error")
    with ledger_path.open("a") as f:      # a charge under this key for a different end location
        f.write(json.dumps({"ts": "2026-11-10T00:00:00.000Z", "idempotency_key": key, "attempt": 9,
                            "outcome": "success", "charged": True, "txn_id": "other", "scooter_id": "18",
                            "reservation_id": key, "end_lat": 9.0, "end_lng": 9.0, "amount": 1}) + "\n")
    before = db_path.read_bytes()
    [result] = reconcile.run()["results"]
    assert result["action"] == "report" and db_path.read_bytes() == before


def test_an_unreadable_ledger_fails_closed(client, monkeypatch, db_path, ledger_path):
    start_and_fail(client, monkeypatch, fault="error")
    ledger_path.write_text(ledger_path.read_text() + "{partial")
    before_db, before_ledger = db_path.read_bytes(), ledger_path.read_bytes()
    summary = reconcile.run()
    # every reservation (18's pending ride and 33's legacy one) is reported: nothing can be checked
    assert {r["action"] for r in summary["results"]} == {"report"} and "18" in summary["reported"]
    assert all("ledger unreadable" in r["reason"] for r in summary["results"])
    assert db_path.read_bytes() == before_db and ledger_path.read_bytes() == before_ledger
    assert reconcile.remaining(summary)


# ---- safety: dry run, idempotency, concurrency ---------------------------------------------

def test_dry_run_changes_nothing_and_writes_no_records(client, monkeypatch, db_path, ledger_path, records_path):
    start_and_fail(client, monkeypatch, fault="error")
    before = (db_path.read_bytes(), ledger_path.read_bytes(), records_path.read_bytes())
    summary = reconcile.run(dry_run=True)
    assert [(r["action"], r["result"]) for r in summary["results"]] == [("retry", "dry_run")]
    assert (db_path.read_bytes(), ledger_path.read_bytes(), records_path.read_bytes()) == before


def test_running_twice_changes_nothing_the_second_time(client, monkeypatch, db_path, ledger_path, records_path, check_all):
    start_and_fail(client, monkeypatch, fault="timeout_charged")
    first = reconcile.run()
    assert not reconcile.remaining(first)
    snapshot = (db_path.read_bytes(), ledger_path.read_bytes(), records_path.read_bytes())
    second = reconcile.run()
    assert second["results"] == [] and not reconcile.remaining(second)
    assert (db_path.read_bytes(), ledger_path.read_bytes(), records_path.read_bytes()) == snapshot
    check_all()


def test_only_one_scooter(client, monkeypatch, db_path):
    start_and_fail(client, monkeypatch, sid="18")
    start_and_fail(client, monkeypatch, sid="15")
    summary = reconcile.run(only="15")
    assert [r["scooter_id"] for r in summary["results"]] == ["15"] and summary["still_pending"] == ["18"]


def test_an_unknown_payment_state_is_reported_not_touched(db_path):
    data = json.loads(db_path.read_text())
    data[4]["reservation"] = {"reservation_id": "a" * 32, "idempotency_key": "a" * 32, "started_at": None,
                              "start_request_id": None, "payment_state": "refund_requested"}
    db_path.write_text(json.dumps(data))
    before = db_path.read_bytes()
    summary = reconcile.run()
    [result] = summary["results"]
    assert (result["action"], result["status"], result["error_kind"]) == ("report", 500, "storage_error")
    assert db_path.read_bytes() == before and reconcile.remaining(summary)


_RIDER = """
import json, random, sys
sys.path.insert(0, sys.argv[1])
import app
rng = random.Random(int(sys.argv[2]))
c = app.app.test_client()
codes = []
for _ in range(int(sys.argv[3])):
    sid = rng.choice(["15", "18", "9", "14", "33"])
    if rng.random() < 0.45:
        codes.append(c.get(f"/reservation/start?id={sid}").status_code)
    else:
        codes.append(c.get(f"/reservation/end?id={sid}&lat={rng.uniform(-60, 60):.3f}&lng={rng.uniform(-60, 60):.3f}").status_code)
print(json.dumps(codes))
"""

_RECONCILER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import reconcile
codes = [reconcile.main(["--json"]) for _ in range(int(sys.argv[2]))]
print(json.dumps(codes))
"""


def _spawn(script, *args, env):
    return subprocess.Popen([sys.executable, "-c", script, str(REPO), *map(str, args)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait(procs):
    outs = [p.communicate(timeout=300) for p in procs]
    assert all(p.returncode == 0 for p in procs), [err[-2000:] for _, err in outs]
    return outs


def _settle(client, monkeypatch, db_path, runs=5):
    """Reconcile with a healthy gateway until nothing is pending (each run: one attempt per ride)."""
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    for _ in range(runs):
        summary = reconcile.run()
        if not reconcile.remaining(summary):
            return summary
    raise AssertionError(f"still not settled: {summary}")


def test_reconcile_runs_safely_alongside_live_requests(client, monkeypatch, db_path, check_all):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PAYMENT_FAULT="random", PAYMENT_SEED="15",
               PAYMENT_FAULT_RATE="0.5", SCOOTER_DB_LOCK_TIMEOUT="60")
    riders = [_spawn(_RIDER, seed, 80, env=env) for seed in range(3)]
    reconciler = _spawn(_RECONCILER, 15, env=env)
    _wait(riders + [reconciler])
    check_all()                                                    # mid-way: still consistent
    _settle(client, monkeypatch, db_path)
    assert not any((s.get("reservation") or {}).get("payment_state") == "payment_pending"
                   for s in json.loads(db_path.read_text()))
    check_all()


# ---- the extended soak: "no scooter stuck reserved forever", with evidence -------------------

def test_after_a_random_fault_soak_reconcile_leaves_nothing_stuck(client, monkeypatch, db_path, read_ledger,
                                                                  read_records, check_all):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PAYMENT_FAULT="random", PAYMENT_SEED="2026",
               PAYMENT_FAULT_RATE="0.5", SCOOTER_DB_LOCK_TIMEOUT="60")
    outs = _wait([_spawn(_RIDER, seed, 125, env=env) for seed in range(4)])
    assert sum(len(json.loads(out)) for out, _ in outs) == 500
    # Whatever the soak left, make sure every kind of stuck ride exists: each free scooter
    # gets a ride whose end fails one way or another.
    faults = iter(["timeout_charged", "timeout_not_charged", "error", "timeout_charged", "error"])
    for entry in json.loads(db_path.read_text()):
        if not entry["is_reserved"]:
            assert client.get(f"/reservation/start?id={entry['id']}").status_code == 200
            monkeypatch.setenv("PAYMENT_FAULT", next(faults))
            client.get(f"/reservation/end?id={entry['id']}&lat=10&lng=10")
    pending_before = [s["id"] for s in json.loads(db_path.read_text())
                      if (s.get("reservation") or {}).get("payment_state") == "payment_pending"]
    assert len(pending_before) >= 2, "reconcile should have stuck rides to settle"
    # Reconcile under the SAME random faults: the terminal rule must still end every ride.
    monkeypatch.setenv("PAYMENT_FAULT", "random")
    monkeypatch.setenv("PAYMENT_SEED", "2026")
    monkeypatch.setenv("PAYMENT_FAULT_RATE", "0.5")
    for _ in range(reconcile.DEFAULT_MAX_ATTEMPTS + 1):
        summary = reconcile.run()
        if not summary["still_pending"]:
            break
    assert summary["still_pending"] == [] and summary["reported"] == []
    db = json.loads(db_path.read_text())
    assert not any((s.get("reservation") or {}).get("payment_state") == "payment_pending" for s in db)
    check_all()                                    # I1-I5, anomalies and ledger-check all clean
    reconciled = [r for r in read_records() if r["path"] == "/reconcile"]
    actions = {d["outcome"] for r in reconciled for d in r["decisions"] if d["step"] == "reconcile"}
    assert reconciled and actions <= {"retry", "settle_charged", "end_unpaid"}
    assert reconcile.run()["results"] == []        # and a further run has nothing to do


# ---- CLI ---------------------------------------------------------------------------------------

def test_cli_exit_codes_and_output(client, monkeypatch, db_path, capsys):
    assert reconcile.main([]) == 0                                  # nothing stuck
    start_and_fail(client, monkeypatch, fault="error")
    assert reconcile.main(["--dry-run"]) == 1                       # something would be done
    assert "would do retry" in capsys.readouterr().out
    monkeypatch.setenv("PAYMENT_FAULT", "error")
    assert reconcile.main(["--json"]) == 1                          # still pending after the attempt
    summary = json.loads(capsys.readouterr().out)
    assert summary["still_pending"] == ["18"]
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    assert reconcile.main([]) == 0
    with pytest.raises(SystemExit) as exc:
        reconcile.main(["--max-attempts", "x"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        reconcile.main(["--max-attempts", "0"])
    assert exc.value.code == 2


def test_cli_reads_its_limits_from_the_environment(client, monkeypatch, db_path, capsys):
    start_and_fail(client, monkeypatch, fault="error")
    monkeypatch.setenv("RECONCILE_MAX_ATTEMPTS", "1")
    assert reconcile.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["action"] == "end_unpaid"


def test_make_reconcile_runs(db_path):
    out = subprocess.run(["make", "-s", "reconcile", "ARGS=--dry-run"], cwd=REPO, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert out.returncode == 0 and "0 action(s)" in out.stdout


# ---- anomaly rules for reconcile records and per-scooter double charges ----------------------

def test_reconcile_records_are_judged(client, monkeypatch, db_path, records_path):
    start_and_fail(client, monkeypatch, fault="error")
    reconcile.run(max_attempts=1)                                    # ended unpaid, with a balance
    records = list(rq.read_records(records_path))
    assert rq.anomalies(records) == []
    rc = records[-1]
    no_balance = dict(rc["scooter_after"], outstanding_balances=[])
    broken = dict(rc, scooter_after=no_balance, scooter_in_memory=no_balance)
    assert [a["type"] for a in rq.anomalies(records[:-1] + [broken])] == ["reconcile_not_settled"]
    still_reserved = dict(rc, scooter_after=dict(rc["scooter_after"], is_reserved=True))
    assert "reconcile_not_settled" in [a["type"] for a in rq.anomalies(records[:-1] + [still_reserved])]


def test_scooter_double_charge_is_flagged_across_reservations():
    def rec(rid, minute, path, status=200, charged=False, before=None, after=None):
        return {"ts": f"2026-11-10T12:{minute:02d}:00.000Z", "request_id": rid, "path": path, "status": status,
                "scooter_id": "18", "decisions": [{"step": "payment", "outcome": "failed" if not charged else "ok",
                                                   "detail": {"simulator": {"charged": charged, "ledger_txn_id": rid}}}]
                if charged else [], "scooter_before": before, "scooter_after": after, "db_write": {}}
    taken, free = {"is_reserved": True}, {"is_reserved": False}
    start = rec("s", 0, "/reservation/start", before=free, after=taken)
    charge_a = rec("a", 1, "/reservation/end", status=504, charged=True, before=taken, after=taken)
    charge_b = rec("b", 2, "/reservation/end", charged=True, before=taken, after=free)
    assert [a["type"] for a in rq.anomalies([start, charge_a, charge_b])
            if a["type"] == "scooter_double_charge"] == ["scooter_double_charge"]
    # a fresh start resets the count: one charge per ride is fine
    start2 = rec("s2", 3, "/reservation/start", before=free, after=taken)
    charge_c = rec("c", 4, "/reservation/end", charged=True, before=taken, after=free)
    assert "scooter_double_charge" not in [a["type"] for a in rq.anomalies([start, charge_b, start2, charge_c])]
