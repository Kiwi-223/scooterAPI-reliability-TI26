"""A restore replays a declined ride (PLAN PR 15b): never charged and owing, never free.

A DB restore brings back a reservation with its idempotency key. Before PR 15b the gateway
only remembered *charges* per key, so a ride that was declined (ended with a balance), then
restored and ended again, was charged on the replay. Restoring a later snapshot (or never
losing the balance) then left the reservation both charged and owing.

Two fixes, both tested here:
- the gateway replays a decline for the same ride (and is a conflict for another ride), so
  the replay is never charged (`payments.REPLAY_DECLINES`); the pre-15b behaviour is emulated
  by turning it off, which is how the double state below is built;
- reconcile's `clear_paid_balance` marks a balance settled when the ledger shows its
  reservation charged (a charge always wins), and `anomalies` says whether the current DB
  still holds the balance (`still_in_db`, `resolved`).

Restores here are per scooter (one scooter's entry put back from a snapshot taken since its
latest start): a whole-DB restore to before a reservation started also loses that ride's
balance, which nothing can rebuild from the DB (see AGENTS.md, a known hazard).
"""
import datetime
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

import app as app_module
import payments
import reconcile
import records_query as rq
from invariants import pending_amounts, started_keys, violations

T0 = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("PAYMENT_TIMEOUT_DELAY_MS", "0")


@pytest.fixture
def end(client, monkeypatch):
    def _end(sid, lat, lng, fault="none"):
        monkeypatch.setenv("PAYMENT_FAULT", fault)
        resp = client.get(f"/reservation/end?id={sid}&lat={lat}&lng={lng}")
        monkeypatch.setenv("PAYMENT_FAULT", "none")
        return resp
    return _end


@pytest.fixture
def state(db_path, read_ledger, read_records, records_path):
    initial = json.loads(db_path.read_text())

    class State:
        def db(self):
            return json.loads(db_path.read_text())

        def scooter(self, sid):
            return next(s for s in self.db() if s["id"] == sid)

        def violations(self):
            recs = read_records()
            return violations(self.db(), read_ledger(), started_keys(recs, initial), pending_amounts(recs))

        def anomalies(self):
            return rq.anomalies(rq.read_records(records_path), db=self.db())

        def ledger_clean(self):
            result = rq.ledger_check(read_ledger(), read_records(), min_age_s=0)
            return not rq.ledger_problems(result)

        def snapshot(self):
            return db_path.read_bytes()

        def restore(self, snapshot):
            db_path.write_bytes(snapshot)
    return State()


def _charged_and_owed(found):
    return [f for f in found if f["type"] == "charged_and_owed"]


def _declined_then_replayed_into_a_charge(client, end, state, monkeypatch):
    """The pre-15b history: declined, restored, ended again the same way and charged.
    -> (reservation_id, snapshot with the balance, txn_id)"""
    monkeypatch.setattr(payments, "REPLAY_DECLINES", False)          # the gateway before PR 15b
    rid = client.get("/reservation/start?id=18").get_json()["reservation_id"]
    riding = state.snapshot()
    assert end("18", 1, 1, "decline").status_code == 422             # ended with a balance
    owing = state.snapshot()
    state.restore(riding)                                             # the restore replays the ride
    resp = end("18", 1, 1)
    assert resp.status_code == 200                                    # ... and it was charged
    monkeypatch.setattr(payments, "REPLAY_DECLINES", True)           # PR 15b deployed from here on
    return rid, owing, resp.get_json()["txn_id"]


# ---- the double state, both variants, settled by reconcile ------------------------------------

def test_balance_still_in_the_db_is_cleared_by_reconcile(client, end, state, monkeypatch, read_records, capsys):
    rid, owing, txn = _declined_then_replayed_into_a_charge(client, end, state, monkeypatch)
    state.restore(owing)                                              # a later restore brings the balance back
    assert ("I1 a reservation was charged and also owes a balance", rid, None) in state.violations()
    [finding] = _charged_and_owed(state.anomalies())
    assert finding["detail"]["still_in_db"] is True and not finding["resolved"]
    assert rq.main(["--json", "anomalies"]) == 1                      # unresolved: exit 1
    capsys.readouterr()

    summary = reconcile.run()
    [result] = summary["results"]
    assert (result["action"], result["result"], result["txn_id"]) == ("clear_paid_balance", "balance_settled", txn)
    assert not reconcile.remaining(summary) and summary["paid_balances_open"] == []
    [balance] = state.scooter("18")["outstanding_balances"]           # kept for the audit trail ...
    assert balance["reservation_id"] == rid and balance["settled_by_txn_id"] == txn   # ... but settled
    assert balance["settled_reason"] == reconcile.SETTLED_REASON and balance["settled_at"]
    assert state.violations() == [] and state.ledger_clean()
    found = state.anomalies()
    assert rq.unresolved(found) == []
    [finding] = _charged_and_owed(found)                              # history, labelled as such
    assert finding["resolved"] and finding["detail"]["still_in_db"] is False
    assert finding["detail"]["settled_by"] == result["request_id"]
    assert rq.main(["anomalies"]) == 0
    assert "historical: resolved" in capsys.readouterr().out
    record = read_records()[-1]
    assert ("balance", "settled") in [(d["step"], d["outcome"]) for d in record["decisions"]]
    assert [e["charged"] for e in payments.all_entries()] == [False, True]   # nothing new charged
    assert reconcile.run()["results"] == []                           # idempotent


def test_the_invariants_cli_is_how_a_cold_agent_confirms_a_restored_db(client, end, state, monkeypatch):
    # AGENTS.md, Reconcile runbook step 5: the records can't see a restore; the DB-based check can
    _, owing, _ = _declined_then_replayed_into_a_charge(client, end, state, monkeypatch)
    state.restore(owing)
    script = str(Path(__file__).with_name("invariants.py"))
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    assert out.returncode == 1 and "I1 a reservation was charged and also owes a balance" in out.stdout
    reconcile.run()
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("0 invariant violation(s)"), out.stdout + out.stderr


def test_balance_wiped_by_the_restore_is_only_a_historical_flag(client, end, state, monkeypatch):
    rid, _, txn = _declined_then_replayed_into_a_charge(client, end, state, monkeypatch)
    assert "outstanding_balances" not in state.scooter("18") or state.scooter("18")["outstanding_balances"] == []
    assert state.violations() == [] and state.ledger_clean()          # the DB is consistent: paid once
    [finding] = _charged_and_owed(state.anomalies())
    assert finding["resolved"] and finding["detail"]["still_in_db"] is False and finding["detail"]["settled_by"] is None
    assert rq.unresolved(state.anomalies()) == []
    summary = reconcile.run()
    assert summary["results"] == [] and not reconcile.remaining(summary)


def test_without_a_db_a_settled_record_decides(client, end, state, monkeypatch, records_path):
    rid, owing, txn = _declined_then_replayed_into_a_charge(client, end, state, monkeypatch)
    state.restore(owing)
    recs = rq.read_records(records_path)
    [finding] = _charged_and_owed(rq.anomalies(recs))
    assert finding["detail"]["still_in_db"] is None and not finding["resolved"]
    reconcile.run()
    [finding] = _charged_and_owed(rq.anomalies(rq.read_records(records_path)))
    assert finding["detail"]["still_in_db"] is None and finding["resolved"]


def test_an_unreadable_db_leaves_the_finding_unresolved(client, end, state, monkeypatch, tmp_path, capsys):
    _declined_then_replayed_into_a_charge(client, end, state, monkeypatch)
    assert rq.main(["anomalies", "--db", str(tmp_path / "missing.json")]) == 1
    assert "DB not read" in capsys.readouterr().out


def test_a_restore_after_the_clear_reopens_the_balance_and_reconcile_clears_it_again(client, end, state, monkeypatch):
    rid, owing, txn = _declined_then_replayed_into_a_charge(client, end, state, monkeypatch)
    state.restore(owing)
    reconcile.run()
    state.restore(owing)                                              # the open balance is back
    assert not _charged_and_owed(state.anomalies())[0]["resolved"]    # the DB decides, not the old record
    assert [r["action"] for r in reconcile.run()["results"]] == ["clear_paid_balance"]
    assert state.violations() == [] and rq.unresolved(state.anomalies()) == []


def test_one_run_clears_the_balance_and_then_settles_the_stuck_ride(client, end, state, monkeypatch):
    rid, owing, txn = _declined_then_replayed_into_a_charge(client, end, state, monkeypatch)
    state.restore(owing)
    client.get("/reservation/start?id=18")
    end("18", 2, 2, "error")                                          # left payment_pending
    assert state.scooter("18")["reservation"]["payment_state"] == "payment_pending"
    summary = reconcile.run()
    assert [(r["action"], r["result"]) for r in summary["results"]] == [("clear_paid_balance", "balance_settled"),
                                                                        ("retry", "ended_paid")]
    assert not reconcile.remaining(summary)
    assert state.violations() == [] and rq.unresolved(state.anomalies()) == [] and state.ledger_clean()


# ---- since PR 15b: the replay is declined again, never charged ---------------------------------

def test_a_restored_declined_ride_ended_the_same_way_is_declined_again(client, end, state, read_ledger):
    rid = client.get("/reservation/start?id=18").get_json()["reservation_id"]
    riding = state.snapshot()
    assert end("18", 1, 1, "decline").status_code == 422
    state.restore(riding)
    resp = end("18", 1, 1)                                            # no fault: the gateway would charge
    assert resp.status_code == 422 and resp.get_json()["ride_ended"] is True
    assert [(e["outcome"], e["charged"]) for e in read_ledger()] == [("decline", False), ("decline", False)]
    assert read_ledger()[-1]["fault"] == {"source": "ledger", "replayed": "decline"}
    [balance] = state.scooter("18")["outstanding_balances"]
    assert balance["reservation_id"] == rid and balance["reason"] == "payment_declined"
    assert state.violations() == [] and state.anomalies() == [] and state.ledger_clean()


def test_a_restored_declined_ride_ended_elsewhere_is_settled_from_the_ledger(client, end, state, read_ledger):
    rid = client.get("/reservation/start?id=18").get_json()["reservation_id"]
    riding = state.snapshot()
    end("18", 1, 1, "decline")
    state.restore(riding)
    assert end("18", 2, 2).status_code == 409                         # a conflict: still riding, nothing charged
    assert state.scooter("18")["reservation"]["payment_state"] == "active"
    summary = reconcile.run()
    [result] = summary["results"]
    assert (result["action"], result["result"]) == ("settle_declined_from_ledger", "ended_with_balance")
    entry = state.scooter("18")
    assert entry["reservation"] is None and (entry["lat"], entry["lng"]) == (1.0, 1.0)
    [balance] = entry["outstanding_balances"]
    assert (balance["reservation_id"], balance["reason"]) == (rid, "payment_declined")
    assert not any(e["charged"] for e in read_ledger())
    assert state.violations() == [] and state.anomalies() == [] and state.ledger_clean()


def test_a_restored_pending_ride_that_was_declined_is_settled_without_asking_again(client, end, state, read_ledger):
    client.get("/reservation/start?id=18")
    end("18", 1, 1, "error")                                          # payment_pending
    pending = state.snapshot()
    assert end("18", 1, 1, "decline").status_code == 422              # the retry was declined
    state.restore(pending)
    entries = len(read_ledger())
    summary = reconcile.run()
    [result] = summary["results"]
    assert (result["action"], result["result"]) == ("settle_declined", "ended_with_balance")
    assert len(read_ledger()) == entries                              # no gateway call
    assert state.scooter("18")["outstanding_balances"][0]["reason"] == "payment_declined"
    assert state.violations() == [] and state.anomalies() == [] and state.ledger_clean()


# ---- a seeded restore-heavy random history, then reconcile -------------------------------------

FAULTS = ["none", "none", "decline", "decline", "error", "timeout_charged", "timeout_not_charged"]
SCOOTERS = ["15", "18", "9", "14"]


def _random_history(client, end, state, monkeypatch, rng, steps):
    """Random starts, ends (with faults), snapshots and per-scooter restores."""
    snaps = {}                     # scooter -> its DB entries since its latest start
    for _ in range(steps):
        sid = rng.choice(SCOOTERS)
        op = rng.random()
        if op < 0.25:
            if client.get(f"/reservation/start?id={sid}").status_code == 200:
                snaps[sid] = [state.scooter(sid)]
        elif op < 0.6:
            end(sid, rng.choice([1, 2]), rng.choice([1, 2]), rng.choice(FAULTS))
        elif op < 0.75:
            if sid in snaps:
                snaps[sid].append(state.scooter(sid))
        elif sid in snaps:         # restore this scooter's entry from a snapshot since its start
            entry = rng.choice(snaps[sid])
            state.restore(json.dumps([entry if s["id"] == sid else s for s in state.db()]).encode())


# Measured: with the pre-15b gateway these seeds replay a declined ride into a charge whose
# balance a later restore brings back (I1 "charged and also owes" before reconcile).
STILL_IN_DB_SEEDS = {8}


@pytest.mark.parametrize("replay_declines", [False, True], ids=["pre-15b-gateway", "15b-gateway"])
@pytest.mark.parametrize("seed", range(1, 9))
def test_a_restore_heavy_random_history_ends_clean_after_reconcile(client, end, state, monkeypatch, seed, replay_declines):
    monkeypatch.setattr(payments, "REPLAY_DECLINES", replay_declines)
    _random_history(client, end, state, monkeypatch, random.Random(seed), 300)
    before = state.violations()
    monkeypatch.setattr(payments, "REPLAY_DECLINES", True)           # reconcile runs the 15b code
    actions = []
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    for now in (None, lambda: later):                                 # retry once, then the terminal rule
        summary = reconcile.run(now=now)
        actions += [r["action"] for r in summary["results"]]
    assert not reconcile.remaining(summary), summary
    db = state.db()
    assert not any(s.get("reservation") and s["reservation"]["payment_state"] == "payment_pending" for s in db)
    assert state.violations() == [], (seed, before)
    assert rq.unresolved(state.anomalies()) == []
    assert state.ledger_clean()
    if replay_declines:
        assert not _charged_and_owed(state.anomalies())               # never, not even historically
        assert "clear_paid_balance" not in actions
    if seed in STILL_IN_DB_SEEDS and not replay_declines:
        assert ("I1 a reservation was charged and also owes a balance" in [v[0] for v in before]
                and "clear_paid_balance" in actions), (before, actions)


def test_the_seeded_histories_cover_both_variants(client, end, state, monkeypatch):
    # seed 5 (pre-15b): replayed into charges whose balances the restores wiped (historical only)
    monkeypatch.setattr(payments, "REPLAY_DECLINES", False)
    _random_history(client, end, state, monkeypatch, random.Random(5), 300)
    found = _charged_and_owed(state.anomalies())
    assert found and all(f["resolved"] and f["detail"]["still_in_db"] is False for f in found)
