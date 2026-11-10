"""The simulated payment gateway (payments.py, PLAN PR 13): outcomes, the ledger, faults.

The autouse `ledger_path` fixture gives every test an empty ledger and clears PAYMENT_* /
ENABLE_FAULT_INJECTION, so each test sets exactly the faults it wants.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app as app_module
import payments
from failures import FailureKind
from messages import ended_ok
from test_failure_kinds import assert_envelope

REPO = Path(__file__).resolve().parent.parent
HEX32 = re.compile(r"[0-9a-f]{32}")


def start_18(client):
    resp = client.get("/reservation/start?id=18")
    assert resp.status_code == 200
    return resp.get_json()["reservation_id"]        # == the idempotency key


def payment_decisions(record):
    return [d for d in record["decisions"] if d["step"] == "payment"]


# ---- every outcome, end to end ---------------------------------------------------------------

def test_success_ends_the_ride_with_a_string_txn_id(client, read_db, read_ledger, read_records):
    key = start_18(client)
    body = client.get("/reservation/end?id=18&lat=1&lng=1").get_json()
    assert body["msg"] == ended_ok("18")
    assert isinstance(body["txn_id"], str) and HEX32.fullmatch(body["txn_id"])   # README: a string
    assert read_db()["18"]["reservation"] is None
    [entry] = read_ledger()
    assert (entry["idempotency_key"], entry["outcome"], entry["charged"], entry["txn_id"], entry["attempt"]) == \
        (key, "success", True, body["txn_id"], 1)
    assert (entry["scooter_id"], entry["reservation_id"], entry["end_lat"], entry["end_lng"]) == ("18", key, 1.0, 1.0)
    assert entry["amount"] == payment_decisions(read_records()[-1])[0]["detail"]["cost"] > 0
    assert entry["request_id"] == read_records()[-1]["request_id"]


@pytest.mark.parametrize("fault,kind,charged", [
    ("decline", FailureKind.PAYMENT_DECLINED, False),
    ("timeout_not_charged", FailureKind.PAYMENT_TIMEOUT, False),
    ("timeout_charged", FailureKind.PAYMENT_TIMEOUT, True),          # money taken, caller doesn't know
    ("error", FailureKind.PAYMENT_FAILED, False),
])
def test_each_fault_end_to_end(client, monkeypatch, read_db, read_ledger, read_records, fault, kind, charged):
    key = start_18(client)
    before = read_db()
    monkeypatch.setenv("PAYMENT_FAULT", fault)
    assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), kind)
    assert read_db() == before                                       # the ride stays open (PLAN PR 13)
    [entry] = read_ledger()
    assert (entry["outcome"], entry["charged"], entry["idempotency_key"]) == (fault, charged, key)
    assert (entry["txn_id"] is not None) == charged
    detail = payment_decisions(read_records()[-1])[-1]["detail"]
    assert (detail["gateway_outcome"], detail["txn_id"], detail["fault"]) == (fault, None, {"source": "env", "mode": fault})
    assert detail["simulator"] == {"charged": charged, "ledger_txn_id": entry["txn_id"]}


def test_a_retry_after_a_charged_timeout_settles_without_a_second_charge(client, monkeypatch, read_db, read_ledger):
    start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_charged")
    assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == 504
    charge = read_ledger()[0]["txn_id"]
    # Even with the fault still forced, the ledger already has this key's charge.
    body = client.get("/reservation/end?id=18&lat=1&lng=1").get_json()
    assert body["txn_id"] == charge and read_db()["18"]["reservation"] is None
    assert [(e["outcome"], e["charged"], e["attempt"]) for e in read_ledger()] == \
        [("timeout_charged", True, 1), ("already_charged", False, 2)]


def test_a_retry_after_an_uncharged_failure_is_a_new_attempt(client, monkeypatch, read_ledger):
    start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", "decline")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == 200
    assert [(e["outcome"], e["attempt"], e["charged"]) for e in read_ledger()] == [("decline", 1, False), ("success", 2, True)]


# ---- the ride-matching rule ----------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"amount": 101}, {"scooter_id": "15"}, {"reservation_id": "other"},
    {"end_lat": 1.5000001}, {"end_lng": -2.0},                       # same everything, another place
])
def test_a_ledger_hit_for_another_ride_is_a_conflict_never_a_charge(change):
    ride = {"idempotency_key": "k1", "scooter_id": "18", "reservation_id": "r1", "amount": 100,
            "end_lat": 1.5, "end_lng": 2.0}
    first = payments.charge(**ride)
    assert first.outcome == "success" and first.charged
    other = payments.charge(**dict(ride, **change))
    assert (other.outcome, other.ok, other.charged, other.txn_id) == ("conflict", False, False, None)
    assert other.detail["existing_charge"]["txn_id"] == first.txn_id
    again = payments.charge(**ride)                                   # the matching ride still settles
    assert (again.outcome, again.txn_id, again.charged) == ("already_charged", first.txn_id, False)
    assert [e["charged"] for e in payments.lookup("k1")] == [True, False, False]


def test_conflict_through_the_api_leaves_the_ride_open(client, monkeypatch, read_db, read_ledger):
    start_18(client)
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_charged")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    before = read_db()
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    assert_envelope(client.get("/reservation/end?id=18&lat=5&lng=5"), FailureKind.PAYMENT_CONFLICT)
    assert read_db() == before
    assert sum(e["charged"] for e in read_ledger()) == 1


def test_an_equidistant_end_after_a_restore_is_a_conflict_not_a_free_ride(client, db_path, read_db, read_ledger):
    # PR #16's review: scooter 18 starts at (23, 19). North (23.009, 19) and south
    # (22.991, 19) are ~2 km apart but the same distance away, so the same amount. Matching
    # on the amount alone closed the second, different ride with the first ride's charge.
    key = start_18(client)
    snapshot = db_path.read_bytes()
    north = client.get("/reservation/end?id=18&lat=23.009&lng=19")
    assert north.status_code == 200
    db_path.write_bytes(snapshot)                                     # the restore
    assert_envelope(client.get("/reservation/end?id=18&lat=22.991&lng=19"), FailureKind.PAYMENT_CONFLICT)
    assert read_db()["18"]["reservation"]["idempotency_key"] == key   # still open, not moved south
    charged, conflict = read_ledger()
    assert charged["amount"] == conflict["amount"] > 0                # the amounts really are equal
    assert (charged["end_lat"], conflict["end_lat"]) == (23.009, 22.991)
    assert conflict["existing_charge"]["txn_id"] == north.get_json()["txn_id"] and not conflict["charged"]
    # and the genuine retry of the north ride still settles with its own charge
    again = client.get("/reservation/end?id=18&lat=23.009&lng=19")
    assert again.status_code == 200 and again.get_json()["txn_id"] == north.get_json()["txn_id"]


@pytest.mark.parametrize("lat,lng", [("+23.009", "19.0"), ("23.0090", " 19 ")])
def test_the_end_location_is_compared_as_the_app_parses_it(client, db_path, lat, lng):
    start_18(client)
    snapshot = db_path.read_bytes()
    txn = client.get("/reservation/end?id=18&lat=23.009&lng=19").get_json()["txn_id"]
    db_path.write_bytes(snapshot)
    assert client.get(f"/reservation/end?id=18&lat={lat}&lng={lng}").get_json()["txn_id"] == txn


def test_other_keys_are_independent():
    a = payments.charge("ka", "18", "ra", 5, end_lat=1.0, end_lng=2.0)
    b = payments.charge("kb", "18", "rb", 5, end_lat=1.0, end_lng=2.0)
    assert a.charged and b.charged and a.txn_id != b.txn_id and (a.attempt, b.attempt) == (1, 1)


# ---- at most one charge per key, across processes --------------------------------------------

_CHARGER = """
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import payments
sync_dir, me = sys.argv[2], sys.argv[3]
open(os.path.join(sync_dir, "ready-" + me), "w").close()
while not os.path.exists(os.path.join(sync_dir, "go")):
    time.sleep(0.0005)
r = payments.charge("shared-key", "18", "r1", 100, end_lat=1.0, end_lng=2.0, request_id="p" + me)
print(json.dumps({"outcome": r.outcome, "txn_id": r.txn_id, "charged": r.charged}))
"""


def _race(tmp_path, n):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    procs = [subprocess.Popen([sys.executable, "-c", _CHARGER, str(REPO), str(sync_dir), str(i)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(n)]
    deadline = time.monotonic() + 120
    while len(list(sync_dir.glob("ready-*"))) < n:
        assert time.monotonic() < deadline, "children never became ready"
        assert all(p.poll() is None for p in procs), [p.communicate() for p in procs if p.poll() is not None]
        time.sleep(0.01)
    (sync_dir / "go").touch()
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [err for _, err in outs]
    return [json.loads(out) for out, _ in outs]


@pytest.mark.parametrize("round_", range(2))
def test_concurrent_charges_for_one_key_charge_exactly_once(tmp_path, read_ledger, round_):
    results = _race(tmp_path, 8)
    assert sorted(r["outcome"] for r in results) == ["already_charged"] * 7 + ["success"]
    assert len({r["txn_id"] for r in results}) == 1                    # everyone learns the same charge
    ledger = read_ledger()
    assert sum(e["charged"] for e in ledger) == 1
    assert sorted(e["attempt"] for e in ledger) == list(range(1, 9))   # attempts counted under the lock


def test_the_ledger_survives_a_restart(read_ledger):
    # A charge made by another process (a previous server run) is found by this one.
    script = ("import sys; sys.path.insert(0, sys.argv[1]); import payments; "
              "print(payments.charge('k-restart', '18', 'r1', 7, end_lat=1.0, end_lng=2.0).txn_id)")
    out = subprocess.run([sys.executable, "-c", script, str(REPO)], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                         capture_output=True, text=True, check=True)
    earlier = out.stdout.strip()
    assert HEX32.fullmatch(earlier)
    now = payments.charge("k-restart", "18", "r1", 7, end_lat=1.0, end_lng=2.0)
    assert (now.outcome, now.txn_id, now.attempt) == ("already_charged", earlier, 2)


# ---- reproducible faults ---------------------------------------------------------------------

def test_the_draw_is_a_pure_function_of_seed_key_and_attempt():
    assert payments.draw("7", "k", 1, 0.5) == payments.draw("7", "k", 1, 0.5)
    first = [payments.draw("7", "k", n, 0.5)[0] for n in range(1, 41)]
    assert first == [payments.draw("7", "k", n, 0.5)[0] for n in range(1, 41)]
    assert first != [payments.draw("8", "k", n, 0.5)[0] for n in range(1, 41)]    # the seed matters
    assert first != [payments.draw("7", "k2", n, 0.5)[0] for n in range(1, 41)]   # so does the key


def test_attempts_are_independent_draws_at_the_configured_rate():
    outcomes = [payments.draw("s", "key", n, 0.3)[0] for n in range(1, 4001)]
    faults = [o for o in outcomes if o != "success"]
    assert 0.25 < len(faults) / len(outcomes) < 0.35                  # ~ the rate
    assert set(faults) == set(payments.FAULTS)                         # every fault occurs
    assert len(set(outcomes[:10])) > 1                                 # a retry isn't stuck on one outcome
    assert all(payments.draw("s", "key", n, 0.0)[0] == "success" for n in range(1, 50))
    assert all(payments.draw("s", "key", n, 1.0)[0] != "success" for n in range(1, 50))


def _end_33_until_settled(client, max_tries=30):
    outcomes = []
    for _ in range(max_tries):
        resp = client.get("/reservation/end?id=33&lat=24&lng=-27")
        outcomes.append(resp.status_code)
        if resp.status_code == 200:
            return outcomes
    raise AssertionError(f"never settled: {outcomes}")


def test_a_seeded_run_replays_exactly(client, monkeypatch, db_path, ledger_path, read_ledger, read_records):
    # Scooter 33's legacy key is fixed ("legacy-33"), so the same seed gives the same
    # sequence of outcomes on a fresh ledger, whichever process serves it.
    monkeypatch.setenv("PAYMENT_FAULT", "random")
    monkeypatch.setenv("PAYMENT_SEED", "42")
    monkeypatch.setenv("PAYMENT_FAULT_RATE", "0.7")
    monkeypatch.setenv("PAYMENT_TIMEOUT_DELAY_MS", "0")
    snapshot = db_path.read_bytes()
    first = _end_33_until_settled(client)
    first_ledger = [(e["attempt"], e["outcome"]) for e in read_ledger()]
    assert len(first) > 1, "pick a seed whose first attempt fails, or this test proves little"
    # every record carries the draw's inputs, and replaying them gives the same outcome
    for r in read_records():
        detail = payment_decisions(r)[-1]["detail"]
        f = detail["fault"]
        if detail["gateway_outcome"] == "already_charged":           # after a charged timeout
            assert f == {"source": "ledger"}
            continue
        assert f["source"] == "env:random" and (f["seed"], f["idempotency_key"], f["rate"]) == ("42", "legacy-33", 0.7)
        assert payments.draw(f["seed"], f["idempotency_key"], f["attempt"], f["rate"])[0] == detail["gateway_outcome"]
    # replay from scratch: same DB, empty ledger
    db_path.write_bytes(snapshot)
    ledger_path.rename(ledger_path.with_suffix(".old"))
    assert _end_33_until_settled(client) == first
    assert [(e["attempt"], e["outcome"]) for e in read_ledger()] == first_ledger


# ---- the override header ---------------------------------------------------------------------

def test_the_header_is_ignored_unless_fault_injection_is_enabled(client, read_records, read_ledger):
    start_18(client)
    resp = client.get("/reservation/end?id=18&lat=1&lng=1", headers={"X-Payment-Fault": "decline"})
    assert resp.status_code == 200                                     # not declined
    note = [d for d in read_records()[-1]["decisions"] if d["step"] == "payment_fault_override"]
    assert note == [{"step": "payment_fault_override", "outcome": "ignored",
                     "detail": {"value": "decline", "reason": "ENABLE_FAULT_INJECTION is not 1"}}]
    assert read_ledger()[0]["fault"] == {"source": "env", "mode": "none"}


@pytest.mark.parametrize("enabled", ["0", "true", "yes", ""])
def test_only_exactly_1_enables_the_header(client, monkeypatch, enabled):
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", enabled)
    start_18(client)
    assert client.get("/reservation/end?id=18&lat=1&lng=1", headers={"X-Payment-Fault": "error"}).status_code == 200


def test_the_header_forces_a_fault_when_enabled_and_beats_the_env(client, monkeypatch, read_records, read_ledger):
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    monkeypatch.setenv("PAYMENT_FAULT", "error")
    start_18(client)
    assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1", headers={"X-Payment-Fault": "decline"}),
                    FailureKind.PAYMENT_DECLINED)
    assert read_ledger()[0]["fault"] == {"source": "header", "mode": "decline"}
    note = [d for d in read_records()[-1]["decisions"] if d["step"] == "payment_fault_override"]
    assert note == [{"step": "payment_fault_override", "outcome": "applied", "detail": {"value": "decline"}}]
    # `none` through the header switches the env's fault off for this request
    assert client.get("/reservation/end?id=18&lat=1&lng=1", headers={"X-Payment-Fault": "none"}).status_code == 200


def test_the_header_can_ask_for_a_seeded_draw(client, monkeypatch, read_ledger):
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    monkeypatch.setenv("PAYMENT_SEED", "3")
    monkeypatch.setenv("PAYMENT_TIMEOUT_DELAY_MS", "0")
    key = start_18(client)
    client.get("/reservation/end?id=18&lat=1&lng=1", headers={"X-Payment-Fault": "random"})
    entry = read_ledger()[0]
    assert entry["fault"]["source"] == "header:random"
    assert entry["outcome"] == payments.draw("3", key, 1, payments.DEFAULT_FAULT_RATE)[0]


def test_an_unknown_header_value_is_rejected_and_the_env_applies(client, monkeypatch, read_records):
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    start_18(client)
    assert client.get("/reservation/end?id=18&lat=1&lng=1", headers={"X-Payment-Fault": "explode"}).status_code == 200
    [note] = [d for d in read_records()[-1]["decisions"] if d["step"] == "payment_fault_override"]
    assert note["outcome"] == "rejected" and note["detail"]["value"] == "explode"


# ---- configuration and failure handling ------------------------------------------------------

@pytest.mark.parametrize("name,value", [
    ("PAYMENT_FAULT", "bogus"), ("PAYMENT_FAULT_RATE", "2"), ("PAYMENT_FAULT_RATE", "x"),
])
def test_a_bad_setting_fails_loudly_and_charges_nothing(client, monkeypatch, read_db, read_ledger, caplog, name, value):
    start_18(client)
    if name == "PAYMENT_FAULT_RATE":
        monkeypatch.setenv("PAYMENT_FAULT", "random")
    monkeypatch.setenv(name, value)
    before = read_db()
    assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.INTERNAL_ERROR)
    assert read_db() == before and read_ledger() == []
    assert "PaymentConfigError" in caplog.text and name in caplog.text


def test_an_unreadable_ledger_fails_closed(client, ledger_path, read_db):
    start_18(client)
    ledger_path.write_text('{"idempotency_key": "x", "charged": true}\n{not json\n')
    before_ledger, before_db = ledger_path.read_bytes(), read_db()
    assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.PAYMENT_FAILED)
    assert ledger_path.read_bytes() == before_ledger and read_db() == before_db


def test_timeouts_wait_only_the_configured_short_delay(monkeypatch):
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_not_charged")
    t0 = time.perf_counter()
    payments.charge("k1", "18", "r", 1, end_lat=1.0, end_lng=2.0)
    assert time.perf_counter() - t0 < 1                               # default: 20 ms, never a real wait
    monkeypatch.setenv("PAYMENT_TIMEOUT_DELAY_MS", "150")
    t0 = time.perf_counter()
    payments.charge("k2", "18", "r", 1, end_lat=1.0, end_lng=2.0)
    assert time.perf_counter() - t0 >= 0.15
    monkeypatch.setenv("PAYMENT_FAULT", "decline")                    # only timeouts wait
    t0 = time.perf_counter()
    payments.charge("k3", "18", "r", 1, end_lat=1.0, end_lng=2.0)
    assert time.perf_counter() - t0 < 0.15


# ---- CLI -------------------------------------------------------------------------------------

def test_cli_draw_replays_a_record(capsys):
    assert payments.main(["draw", "--seed", "42", "--key", "legacy-33", "--attempt", "2", "--rate", "0.7"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == payments.draw("42", "legacy-33", 2, 0.7)[0]


def test_cli_lookup(capsys):
    payments.charge("k-cli", "18", "r", 3, end_lat=1.0, end_lng=2.0)
    assert payments.main(["lookup", "k-cli"]) == 0
    assert json.loads(capsys.readouterr().out)["charged"] is True
    assert payments.main(["lookup", "nope"]) == 1


def test_cli_runs_as_a_module():
    out = subprocess.run([sys.executable, "-m", "payments", "draw", "--seed", "1", "--key", "k", "--attempt", "1"],
                         cwd=REPO, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert out.returncode == 0 and json.loads(out.stdout)["outcome"] in payments.OUTCOMES


# ---- verify: the unreadable-ledger runbook's first step ---------------------------------------

def _ledger_with_one_charge(ledger_path):
    payments.charge("k-ok", "18", "r", 3, end_lat=1.0, end_lng=2.0)
    return ledger_path.read_text()


def test_verify_a_good_ledger(ledger_path, capsys):
    _ledger_with_one_charge(ledger_path)
    assert payments.main(["verify"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"path": str(ledger_path), "exists": True, "entries": 1, "ok": True, "problem": None}


def test_verify_a_missing_ledger_is_fine(ledger_path, capsys):
    assert payments.main(["verify"]) == 0
    assert json.loads(capsys.readouterr().out)["exists"] is False


def test_verify_finds_an_interrupted_append(ledger_path, capsys):
    good = _ledger_with_one_charge(ledger_path)
    ledger_path.write_text(good + '{"ts": "2026-11-10T00:00:00.000Z", "idempotency_key": "k-cut", "att')
    assert payments.main(["verify"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["problem"] == {"line": 2, "reason": "not valid JSON", "last_line": True,
                                              "missing_newline": True, "interrupted_append": True}
    assert "line 2" in out.err and "interrupted append" in out.err
    # the documented repair: keep the good lines, drop the partial one -> verify passes again
    ledger_path.write_text(good)
    assert payments.main(["verify"]) == 0


@pytest.mark.parametrize("bad", ["{not json}\n", "[1, 2]\n", '{"no_key": 1}\n'])
def test_verify_flags_a_bad_line_in_the_middle_as_not_repairable_by_hand(ledger_path, capsys, bad):
    good = _ledger_with_one_charge(ledger_path)
    ledger_path.write_text(bad + good)
    assert payments.main(["verify"]) == 1
    out = capsys.readouterr()
    problem = json.loads(out.out)["problem"]
    assert (problem["line"], problem["interrupted_append"]) == (1, False)
    assert "escalate" in out.err


def test_verify_runs_through_make(ledger_path):
    _ledger_with_one_charge(ledger_path)
    out = subprocess.run(["make", "-s", "payments", "ARGS=verify"], cwd=REPO, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert out.returncode == 0 and json.loads(out.stdout)["entries"] == 1
