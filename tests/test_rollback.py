"""What a DB rollback erases, and what reconcile recovers (PLAN PR 15c).

A rollback (`git restore scooter_db.json`, a backup) puts back an older DB *whole*. The
ledger and the records survive it. Everything the DB learned since is gone:
- outstanding balances (a declined ride, or one reconcile ended unpaid): the ledger still
  proves the debt when it has a decline or uncharged attempts -> reconcile's
  rebuild_lost_balance re-creates it;
- rides in progress (started after the snapshot, not ended): nothing in the ledger, so
  nobody pays: a free ride, reported by `records_query rollback-check`;
- a balance whose ride never reached the gateway (end_unpaid by age): records only,
  reported, not rebuilt;
- settled markers and rebuilt balances: reconcile redoes them (15b / 15c).
"""
import datetime
import json
import random

import pytest

import payments
import reconcile
import records_query as rq
from invariants import pending_amounts, started_keys, violations

LATER = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
I2 = "I2 ended without a charge or an outstanding balance"


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
def world(db_path, read_ledger, read_records, records_path):
    initial = json.loads(db_path.read_text())

    class World:
        def db(self):
            return json.loads(db_path.read_text())

        def scooter(self, sid):
            return next(s for s in self.db() if s["id"] == sid)

        def violations(self):
            recs = read_records()
            return violations(self.db(), read_ledger(), started_keys(recs, initial), pending_amounts(recs))

        def i2_keys(self):
            return {v[1] for v in self.violations() if v[0] == I2}

        def rollback(self):
            return rq.rollback_check(read_records(), self.db(), read_ledger())

        def unresolved_anomalies(self):
            return rq.unresolved(rq.anomalies(rq.read_records(records_path), db=self.db()))

        def ledger_clean(self):
            return not rq.ledger_problems(rq.ledger_check(read_ledger(), read_records(), min_age_s=0))

        def snapshot(self):
            return db_path.read_bytes()

        def restore(self, snapshot):
            db_path.write_bytes(snapshot)
    return World()


def _start(client, sid="18"):
    resp = client.get(f"/reservation/start?id={sid}")
    assert resp.status_code == 200
    return resp.get_json()["reservation_id"]


def _balances(world, sid="18"):
    return world.scooter(sid).get("outstanding_balances") or []


# ---- 1. lost balances are rebuilt from the ledger -----------------------------------------------

def test_a_declined_balance_erased_by_a_rollback_is_rebuilt(client, end, world, read_ledger, read_records, capsys):
    before = world.snapshot()
    rid = _start(client)
    assert end("18", 1, 1, "decline").status_code == 422
    [original] = _balances(world)
    world.restore(before)                                         # the rollback: ride and balance gone
    assert world.i2_keys() == {rid}
    result = world.rollback()
    assert [(b["reservation_id"], b["original_reason"]) for b in result["lost_balances"]] == [(rid, "payment_declined")]
    assert result["erased_rides"] == [] and rq.rollback_problems(result)
    assert rq.main(["rollback-check"]) == 1
    capsys.readouterr()

    summary = reconcile.run()
    [r] = summary["results"]
    assert (r["action"], r["result"]) == ("rebuild_lost_balance", "balance_rebuilt")
    assert not reconcile.remaining(summary) and summary["lost_balances_open"] == []
    [balance] = _balances(world)
    assert balance["reason"] == "rebuilt_from_ledger" and balance["original_reason"] == "payment_declined"
    for field in ("reservation_id", "idempotency_key", "amount", "end_lat", "end_lng"):
        assert balance[field] == original[field], field
    assert balance["ended_at"] == read_ledger()[0]["ts"]          # when the ride ended, per the ledger
    assert world.scooter("18")["reservation"] is None and world.scooter("18")["lat"] == 23.0   # not moved
    assert ("balance", "rebuilt") in [(d["step"], d["outcome"]) for d in read_records()[-1]["decisions"]]
    assert world.violations() == [] and world.unresolved_anomalies() == [] and world.ledger_clean()
    result = world.rollback()
    assert not rq.rollback_problems(result) and [b["reservation_id"] for b in result["rebuilt_balances"]] == [rid]
    assert rq.main(["rollback-check"]) == 0
    assert "balances already rebuilt by reconcile: 1" in capsys.readouterr().out
    assert reconcile.run()["results"] == []                       # idempotent
    assert len(read_ledger()) == 1                                # never asked the gateway


def test_a_rollback_of_the_rebuild_is_rebuilt_again(client, end, world):
    before = world.snapshot()
    _start(client)
    end("18", 1, 1, "decline")
    world.restore(before)
    reconcile.run()
    world.restore(before)                                         # erases the rebuilt balance too
    assert [r["action"] for r in reconcile.run()["results"]] == ["rebuild_lost_balance"]
    assert len(_balances(world)) == 1 and world.violations() == []


@pytest.mark.parametrize("how", ["ended_unpaid", "still_pending"])
def test_an_unresolved_ride_erased_by_a_rollback_is_rebuilt_from_its_last_attempt(client, end, world, how):
    # payment_unresolved: no decline, only uncharged attempts. The ride can't be retried any
    # more (the DB doesn't have it), so H3's terminal rule applies: it ends with a balance.
    before = world.snapshot()
    rid = _start(client)
    end("18", 1, 1, "error")
    end("18", 9, 9, "timeout_not_charged")                       # a retry: the stored location
    if how == "ended_unpaid":
        assert reconcile.run(max_attempts=1)["results"][0]["action"] == "end_unpaid"
    world.restore(before)
    [lost] = world.rollback()["lost_balances"]
    assert (lost["original_reason"], lost["ledger_outcome"], lost["ledger_attempt"]) == \
        ("payment_unresolved", "timeout_not_charged", 2)
    assert [r["action"] for r in reconcile.run()["results"]] == ["rebuild_lost_balance"]
    [balance] = _balances(world)
    assert (balance["reservation_id"], balance["end_lat"], balance["original_reason"]) == (rid, 1.0, "payment_unresolved")
    assert world.violations() == [] and world.unresolved_anomalies() == [] and world.ledger_clean()


# ---- 2. what can't be recovered is reported -----------------------------------------------------

def test_a_ride_in_progress_erased_by_a_rollback_is_reported_as_a_free_ride(client, world, capsys):
    before = world.snapshot()
    rid = _start(client)                                          # riding when the DB was rolled back
    world.restore(before)
    assert world.i2_keys() == {rid}
    assert reconcile.run()["results"] == []                       # nothing to settle it with
    result = world.rollback()
    assert [(e["reservation_id"], e["scooter_id"]) for e in result["erased_rides"]] == [(rid, "18")]
    assert result["lost_balances"] == [] and result["erased_balances"] == []
    assert rq.main(["rollback-check"]) == 1
    out = capsys.readouterr().out
    assert "FREE RIDES, unrecoverable): 1" in out and rid in out
    assert rq.main(["--json", "rollback-check", "--since", "1h"]) == 1
    assert json.loads(capsys.readouterr().out)["erased_rides"][0]["reservation_id"] == rid
    assert rq.main(["rollback-check", "--since", LATER.strftime("%Y-%m-%dT%H:%M:%SZ")]) == 0     # "the last restore" window
    capsys.readouterr()


def test_a_balance_that_never_reached_the_gateway_is_reported_not_rebuilt(client, world, monkeypatch, read_ledger):
    before = world.snapshot()
    rid = _start(client)
    monkeypatch.setenv("PAYMENT_FAULT", "not-a-mode")             # phase 1 is saved, the gateway refuses to run
    assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == 500
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    assert world.scooter("18")["reservation"]["payment_state"] == "payment_pending" and read_ledger() == []
    assert reconcile.run(now=lambda: LATER)["results"][0]["action"] == "end_unpaid"   # by age, 0 attempts
    world.restore(before)
    assert reconcile.run()["results"] == []                       # no ledger evidence to rebuild from
    result = world.rollback()
    [erased] = result["erased_balances"]
    assert (erased["reservation_id"], erased["reason"]) == (rid, "payment_unresolved") and erased["amount"] > 0
    assert result["erased_rides"] == [] and world.i2_keys() == {rid}


def test_a_charged_ride_erased_by_a_rollback_loses_nothing(client, end, world):
    before = world.snapshot()
    _start(client)
    assert end("18", 1, 1).status_code == 200
    world.restore(before)
    assert world.violations() == [] and not rq.rollback_problems(world.rollback())
    assert reconcile.run()["results"] == []


# ---- 3. the guards ------------------------------------------------------------------------------

def test_never_rebuilt_while_the_reservation_is_live_in_the_db(client, end, world):
    rid = _start(client)
    riding = world.snapshot()
    end("18", 1, 1, "decline")
    world.restore(riding)                                         # active again, the decline in the ledger
    assert world.rollback()["lost_balances"] == []
    actions = [r["action"] for r in reconcile.run()["results"]]
    assert actions == ["settle_declined_from_ledger"]             # settled as a ride, once
    assert [b["reservation_id"] for b in _balances(world)] == [rid] and world.violations() == []


def test_never_rebuilt_for_a_reservation_charged_later(client, end, world, monkeypatch):
    before = world.snapshot()
    _start(client)
    riding = world.snapshot()
    end("18", 1, 1, "decline")
    world.restore(riding)
    monkeypatch.setattr(payments, "REPLAY_DECLINES", False)      # the pre-15b gateway charges the replay
    assert end("18", 1, 1).status_code == 200
    monkeypatch.setattr(payments, "REPLAY_DECLINES", True)
    world.restore(before)                                         # declined AND charged, no trace in the DB
    assert world.rollback()["lost_balances"] == []
    assert reconcile.run()["results"] == []                       # a charge always wins: nothing owed
    assert world.violations() == [] and _balances(world) == []


LEDGER = [{"ts": "t1", "idempotency_key": "r", "reservation_id": "r", "scooter_id": "18", "outcome": "decline",
           "charged": False, "attempt": 1, "end_lat": 1.0, "end_lng": 1.0, "amount": 5}]
FREE = {"id": "18", "lat": 0, "lng": 0, "is_reserved": False, "reservation": None}


def _bal(**over):
    return dict({"reservation_id": "r", "idempotency_key": "r", "amount": 5, "end_lat": 1.0, "end_lng": 1.0,
                 "ended_at": "t", "reason": "payment_declined"}, **over)


@pytest.mark.parametrize("db,ledger,lost", [
    ([FREE], LEDGER, ["r"]),
    ([dict(FREE, outstanding_balances=[_bal()])], LEDGER, []),                               # still owed
    ([dict(FREE, outstanding_balances=[_bal(settled_by_txn_id="t", settled_at="t", settled_reason="x")])],
     LEDGER, []),                                                                           # settled counts
    ([dict(FREE, outstanding_balances=[_bal(reservation_id="other")])], LEDGER, []),         # by key too
    ([dict(FREE, id="9", outstanding_balances=[_bal()])], LEDGER, []),                       # any scooter
    ([dict(FREE, is_reserved=True, reservation={"reservation_id": "r", "idempotency_key": "r",
                                                 "payment_state": "active"})], LEDGER, []),  # live: active
    ([dict(FREE, is_reserved=True, reservation={"reservation_id": "r", "idempotency_key": "r",
                                                 "payment_state": "payment_pending"})], LEDGER, []),
    ([FREE], LEDGER + [dict(LEDGER[0], outcome="success", charged=True, txn_id="x")], []),   # charged later
    ([FREE], LEDGER + [dict(LEDGER[0], idempotency_key="r2", outcome="success", charged=True)], []),  # any key
    ([FREE], [dict(LEDGER[0], outcome="error")], ["r"]),                                     # payment_unresolved
    ([FREE], [dict(LEDGER[0], outcome="conflict")], []),                                     # no evidence
    ([{"id": "18", "lat": 0, "lng": 0, "is_reserved": True}],
     [dict(LEDGER[0], reservation_id="legacy-18", idempotency_key="legacy-18")], []),        # legacy shape: live
])
def test_lost_balances_guards(db, ledger, lost):
    assert [b["reservation_id"] for b in rq.lost_balances(db, ledger)] == lost


def test_decide_rebuilds_only_this_scooters_and_checks_the_whole_db():
    now = datetime.datetime(2026, 11, 10, tzinfo=datetime.UTC)
    assert reconcile.decide(FREE, LEDGER, now)["action"] == "rebuild_lost_balance"
    assert reconcile.decide(dict(FREE, id="9"), LEDGER, now)["action"] == "none"        # the ledger says scooter 18
    other = dict(FREE, id="9", outstanding_balances=[_bal()])
    assert reconcile.decide(FREE, LEDGER, now, db=[FREE, other])["action"] == "none"     # known elsewhere
    assert reconcile.decide(FREE, [dict(LEDGER[0], amount=-1)], now)["action"] == "report"


# ---- 4. seeded whole-DB rollback histories --------------------------------------------------------

FAULTS = ["none", "none", "decline", "decline", "error", "timeout_charged", "timeout_not_charged"]
SCOOTERS = ["15", "18", "9", "14", "33"]


def _history(client, end, world, rng, steps):
    snapshots = [world.snapshot()]
    for _ in range(steps):
        sid = rng.choice(SCOOTERS)
        op = rng.random()
        if op < 0.3:
            client.get(f"/reservation/start?id={sid}")
        elif op < 0.7:
            end(sid, rng.choice([1, 2]), rng.choice([1, 2]), rng.choice(FAULTS))
        elif op < 0.85:
            snapshots.append(world.snapshot())
        elif op < 0.92:
            reconcile.run()                                       # reconcile mid-history too
        else:
            world.restore(rng.choice(snapshots))                  # a whole-DB rollback


@pytest.mark.parametrize("seed", range(1, 9))
def test_after_whole_db_rollbacks_reconcile_leaves_only_reported_free_rides(client, end, world, seed, capsys):
    _history(client, end, world, random.Random(seed), 300)
    lost_before = len(world.rollback()["lost_balances"])
    i2_before = len(world.i2_keys())
    for now in (None, lambda: LATER):
        summary = reconcile.run(now=now)
    assert not reconcile.remaining(summary), summary
    result = world.rollback()
    assert result["lost_balances"] == []
    unrecoverable = {e["idempotency_key"] for e in result["erased_rides"] + result["erased_balances"]}
    # I2's only remaining hits are exactly the reported, unrecoverable ones; everything else holds
    assert world.i2_keys() == unrecoverable, (seed, world.violations(), result)
    assert [v for v in world.violations() if v[0] != I2] == []
    assert world.unresolved_anomalies() == [] and world.ledger_clean()
    print(f"\nseed {seed}: I2 before {i2_before}, lost balances rebuilt {lost_before}, "
          f"erased rides {len(result['erased_rides'])}, erased balances {len(result['erased_balances'])}")


def test_the_seeded_histories_exercise_every_loss(client, end, world):
    # one long history: it must contain each kind of loss, so the test above isn't vacuous
    _history(client, end, world, random.Random(4), 600)
    result = world.rollback()
    assert result["lost_balances"] and result["erased_rides"]
    assert {b["original_reason"] for b in result["lost_balances"]} == {"payment_declined", "payment_unresolved"}


def test_a_lost_balance_reconcile_cant_rebuild_keeps_the_exit_code_at_1(ledger_path, world):
    # its scooter isn't in the DB (e.g. removed from the file): nowhere to put the balance
    ledger_path.write_text(json.dumps(dict(LEDGER[0], scooter_id="99", ts="2026-11-10T00:00:00.000Z")) + "\n")
    summary = reconcile.run()
    assert summary["results"] == [] and summary["lost_balances_open"] == ["r"] and reconcile.remaining(summary)
    assert [b["scooter_id"] for b in world.rollback()["lost_balances"]] == ["99"]          # and it's listed
    assert reconcile.main([]) == 1
