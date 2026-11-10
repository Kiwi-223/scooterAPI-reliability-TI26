"""Reconcile stuck rides (PLAN PR 15; H3): no scooter stays reserved forever.

    make reconcile                       # settle what can be settled now
    make reconcile ARGS="--dry-run"      # show what it would do; changes nothing
    python -m reconcile --help

For every scooter, `decide()` (a pure function of the scooter's DB entry, the ledger and the
time) says what to do; the first matching row wins:

- any scooter, with an open outstanding balance whose reservation the ledger shows charged
  (a DB restore replayed a declined ride into a charge, PLAN PR 15b)  -> clear_paid_balance:
  mark every such balance settled (settled_by_txn_id/settled_at/settled_reason; kept, not
  deleted, for the audit trail). A charge always wins: the rider paid, so they owe nothing.
- payment_pending and the ledger has a charge for the stored ride  -> settle_charged:
  end the ride with that charge (the gateway answers already_charged; nothing new is charged)
- payment_pending, the ledger has a charge for a *different* ride  -> report (never guessed)
- payment_pending, no charge, the ledger has a decline for the stored ride -> settle_declined:
  end it with a payment_declined balance, no new gateway call (a decline is definitive)
- payment_pending, no charge, a decline for a *different* ride     -> report
- payment_pending, no charge, after max_attempts gateway attempts or max_age since payment
  started                                                          -> end_unpaid (H3):
  end the ride with an outstanding balance (reason payment_unresolved), no charge
- payment_pending, no charge, within the limits                   -> retry: one new gateway
  attempt with the same key and the stored location/amount (success / decline / still
  pending, exactly like a rider's retry)
- active, but the ledger has a charge for this reservation (a restored DB: invariant I1)
                                                                   -> settle_from_ledger:
  end the ride where the ledger says it ended, with that txn_id, no new charge; or report
  it if the charge doesn't fit this ride (another scooter or reservation, or an amount that
  doesn't match the distance from where the scooter stands)
- active, no charge, but the ledger has a decline for this reservation (a restored DB)
                                                                   -> settle_declined_from_ledger:
  end the ride where the decline says it ended, with a payment_declined balance, no new
  gateway call; or report it if the decline doesn't fit, as above
- anything else (free, riding)                                     -> none

Each action runs as its own transaction under the DB lock (one ride at a time, like any
request) through a tiny internal Flask app, so it leaves an ordinary request record with path
`/reconcile`: `make records ARGS="scooter <id>"`, `anomalies` and `ledger-check` all see what
reconcile did and why. The decision is re-made inside the lock, on fresh state, so a rider's
request racing reconcile can't make it act on stale data. A run makes up to MAX_PASSES passes,
so a scooter whose paid balance was cleared gets its ride settled in the same run; a scooter
that was retried or reported is not touched again in that run (one gateway attempt per run).

Exit codes: 0 = nothing left (no payment_pending ride, no open balance the ledger shows paid,
nothing reported); 1 = something remains (a ride still pending after this run's attempt, a
reported problem, or an unreadable ledger); 2 = usage.
"""
import argparse
import datetime
import json
import os
import sys
import uuid

from flask import Flask, current_app, request
from geopy.distance import distance as geodesic

import app as app_module
import payments
import records
from failures import FailureKind, StorageBusy, StorageError

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_AGE_MINUTES = 15.0
PATH = "/reconcile"
ACTIONS = ("none", "clear_paid_balance", "retry", "settle_charged", "settle_declined", "end_unpaid",
           "settle_from_ledger", "settle_declined_from_ledger", "report")
MAX_PASSES = 3      # clear_paid_balance, then the ride's own action, then one spare
FINAL_ACTIONS = ("retry", "report")     # never repeated within a run
SETTLED_REASON = "paid: the ledger has a charge for this reservation"


def _env_number(name, default, cast):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise SystemExit(f"reconcile: {name}={raw!r} is not a number") from None
    if value < 0:
        raise SystemExit(f"reconcile: {name}={raw!r} must be >= 0")
    return value


def _parse_ts(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _cost_from(lat, lng, end_lat, end_lng):
    distance = round(geodesic((lat, lng), (end_lat, end_lng)).m)
    return distance, app_module.calculate_cost(distance)


# ---- the decision: pure ------------------------------------------------------------------

def paid_balances(entry, ledger):
    """[(balance, charge)] for each open outstanding balance of this scooter whose reservation
    the ledger shows charged (on this scooter). Pure."""
    found = []
    for balance in entry.get("outstanding_balances") or []:
        if not app_module.is_open_balance(balance):
            continue
        charge = next((e for e in ledger if e.get("charged") is True and e.get("scooter_id") == entry["id"]
                       and e.get("reservation_id") == balance.get("reservation_id")), None)
        if charge is not None:
            found.append((balance, charge))
    return found


def _ride_fits(entry, reservation, ledger_entry, what):
    """For an active ride the ledger says already ended: (None, settle fields) if the ledger's
    ride fits this scooter and reservation, else (report reason, None)."""
    if (ledger_entry.get("scooter_id"), ledger_entry.get("reservation_id")) != (entry["id"], reservation["reservation_id"]):
        return f"the ledger's {what} for this key is for another scooter or reservation", None
    try:
        distance, cost = _cost_from(entry["lat"], entry["lng"], ledger_entry["end_lat"], ledger_entry["end_lng"])
    except (KeyError, TypeError, ValueError):
        return f"the ledger's {what} has no usable end location", None
    if cost != ledger_entry.get("amount"):
        return f"{what}, but the amount doesn't match the distance from where the scooter stands", None
    return None, {"end_lat": ledger_entry["end_lat"], "end_lng": ledger_entry["end_lng"], "distance_m": distance, "amount": cost}


def decide(entry, ledger, now, max_attempts=DEFAULT_MAX_ATTEMPTS, max_age=datetime.timedelta(minutes=DEFAULT_MAX_AGE_MINUTES)):
    """What reconcile should do about one scooter -> {"action", "reason", ...}.

    entry: the scooter as persisted (`Scooter.to_dict()`, so a legacy reservation is already
    synthesised); ledger: the ledger's entries (all of them, or at least those for this
    scooter's reservation key and balances); now: an aware datetime. Pure: no I/O, no clock.
    """
    paid = paid_balances(entry, ledger)
    if paid:
        return {"action": "clear_paid_balance", "reason": "outstanding balance for a reservation the ledger shows charged",
                "txn_id": paid[0][1].get("txn_id"),
                "balances": [{"reservation_id": b["reservation_id"], "amount": b.get("amount"), "txn_id": c.get("txn_id"),
                              "charged_amount": c.get("amount")} for b, c in paid]}
    reservation = entry.get("reservation")
    if reservation is None:
        return {"action": "none", "reason": "free"}
    key_entries = [e for e in ledger if e.get("idempotency_key") == reservation["idempotency_key"]]
    charges = [e for e in key_entries if e.get("charged") is True]
    declines = [e for e in key_entries if e.get("outcome") == "decline"]
    state = reservation.get("payment_state")
    if state == "payment_pending":
        if charges:
            charge = charges[0]
            if payments._matches(charge, entry["id"], reservation["reservation_id"], reservation["end_lat"],
                                 reservation["end_lng"], reservation["amount"]):
                return {"action": "settle_charged", "reason": "the ledger has a charge for this ride",
                        "txn_id": charge.get("txn_id"), "end_lat": reservation["end_lat"], "end_lng": reservation["end_lng"]}
            return {"action": "report", "reason": "the ledger has a charge under this key for a different ride",
                    "txn_id": charge.get("txn_id")}
        if declines:
            # declined before (only a DB restore gets a declined ride back to pending): the
            # gateway would only replay the decline, so settle it without asking again
            if payments._matches(declines[0], entry["id"], reservation["reservation_id"], reservation["end_lat"],
                                 reservation["end_lng"], reservation["amount"]):
                return {"action": "settle_declined", "reason": "the ledger has a decline for this ride",
                        "end_lat": reservation["end_lat"], "end_lng": reservation["end_lng"]}
            return {"action": "report", "reason": "the ledger has a decline under this key for a different ride"}
        attempts = max(reservation.get("payment_attempts") or 0, len(key_entries))
        started = _parse_ts(reservation.get("payment_requested_at"))
        stored = {"end_lat": reservation["end_lat"], "end_lng": reservation["end_lng"]}
        if attempts >= max_attempts:
            return {"action": "end_unpaid", "attempts": attempts, **stored,
                    "reason": f"no charge after {attempts} attempt(s) (limit {max_attempts})"}
        if started is not None and now - started >= max_age:
            minutes = round((now - started).total_seconds() / 60, 1)
            return {"action": "end_unpaid", "attempts": attempts, **stored,
                    "reason": f"no charge {minutes} min after payment started (limit {max_age.total_seconds() / 60:g} min)"}
        return {"action": "retry", "attempts": attempts, "reason": f"no charge yet; attempt {attempts + 1} of {max_attempts}"}
    if state == "active":
        if charges:
            charge = charges[0]
            problem, ride = _ride_fits(entry, reservation, charge, "charge")
            if problem:
                return {"action": "report", "reason": problem, "txn_id": charge.get("txn_id")}
            return {"action": "settle_from_ledger", "txn_id": charge.get("txn_id"),
                    "reason": "active, but already charged for this reservation (a restored DB)", **ride}
        if declines:
            problem, ride = _ride_fits(entry, reservation, declines[0], "decline")
            if problem:
                return {"action": "report", "reason": problem}
            return {"action": "settle_declined_from_ledger",
                    "reason": "active, but already declined for this reservation (a restored DB)", **ride}
        return {"action": "none", "reason": "riding"}
    return {"action": "report", "reason": f"payment_state {state!r} isn't handled here"}


def ledger_entries():
    """Every ledger entry -> (entries, None) or (None, problem). Fails closed."""
    try:
        return payments.all_entries(), None
    except (OSError, ValueError) as exc:
        return None, f"ledger unreadable: {exc}"


def plan(db, now, max_attempts, max_age):
    """[(scooter_id, decision)] for every scooter that needs something (read-only)."""
    result = []
    entries, problem = ledger_entries()
    for scooter in db:
        entry = scooter.to_dict()
        if entry.get("reservation") is None and not any(map(app_module.is_open_balance, entry.get("outstanding_balances") or [])):
            continue
        decision = ({"action": "report", "reason": problem} if problem
                    else decide(entry, entries, now, max_attempts, max_age))
        if decision["action"] != "none":
            result.append((scooter.id, decision))
    return result


# ---- one action, as a recorded request under the DB lock -----------------------------------

def _respond(status, body):
    return json.dumps(body), status, {"Content-Type": "application/json"}


def _report(scooter_id, reason):
    # left for a human: a recorded failure (reconcile_unsettled), so recent-failures and
    # failure-counts show it, with what reconcile decided and why
    return app_module.error_response(FailureKind.RECONCILE_UNSETTLED,
                                     f"Reconcile left scooter {scooter_id} unchanged: {reason}.",
                                     extra={"scooter_id": scooter_id, "action": "report", "reason": reason,
                                            "result": "unchanged"})


def _reconcile_one():
    scooter_id = request.args["id"]
    records.scooter(scooter_id=scooter_id)
    cfg = current_app.config
    with app_module.db_transaction() as txn:
        scooter = app_module.find_scooter(scooter_id, txn.db)
        if not scooter:
            records.note("reconcile", "skipped", reason="no such scooter")
            return _respond(404, {"scooter_id": scooter_id, "action": "none", "result": "no such scooter"})
        app_module.refuse_unsupported_reservation(scooter, txn.path)
        reservation = scooter.reservation
        entries, problem = ledger_entries()
        decision = ({"action": "report", "reason": problem} if problem
                    else decide(scooter.to_dict(), entries, cfg["RECONCILE_NOW"](), cfg["RECONCILE_MAX_ATTEMPTS"],
                                cfg["RECONCILE_MAX_AGE"]))
        action = decision["action"]
        records.note("reconcile", action, **{k: v for k, v in decision.items() if k != "action"})
        body = {"scooter_id": scooter_id, **decision}
        if action == "none":
            records.scooter(in_memory=scooter.to_dict())
            records.unchanged()
            return _respond(200, dict(body, result="unchanged"))
        if action == "report":
            records.scooter(in_memory=scooter.to_dict())
            records.unchanged()
            return _report(scooter_id, decision["reason"])
        if action == "clear_paid_balance":
            settled_at = app_module.utc_now_iso()
            by_reservation = {b["reservation_id"]: b["txn_id"] for b in decision["balances"]}
            for balance in scooter.outstanding_balances:
                if app_module.is_open_balance(balance) and balance["reservation_id"] in by_reservation:
                    balance.update({"settled_by_txn_id": by_reservation[balance["reservation_id"]],
                                    "settled_at": settled_at, "settled_reason": SETTLED_REASON})
                    records.note("balance", "settled", reservation_id=balance["reservation_id"],
                                 txn_id=balance["settled_by_txn_id"], amount=balance["amount"])
            app_module.write_db(txn.db, txn.path)
            records.scooter(in_memory=scooter.to_dict())
            return _respond(200, dict(body, result="balance_settled"))
        if action == "settle_declined":
            amount = reservation["amount"]
            app_module.record_outstanding_balance(scooter)
            app_module.finish_ride(scooter)
            app_module.write_db(txn.db, txn.path)
            records.scooter(in_memory=scooter.to_dict())
            return _respond(200, dict(body, result="ended_with_balance", outstanding_balance=amount))
        if action == "end_unpaid":
            amount = reservation["amount"]
            app_module.record_outstanding_balance(scooter, reason="payment_unresolved")
            app_module.finish_ride(scooter)
            app_module.write_db(txn.db, txn.path)
            records.scooter(in_memory=scooter.to_dict())
            return _respond(200, dict(body, result="ended_with_balance", outstanding_balance=amount))
        if action in ("settle_from_ledger", "settle_declined_from_ledger"):
            reservation.update({"payment_state": "payment_pending", "end_lat": decision["end_lat"],
                                "end_lng": decision["end_lng"], "distance_m": decision["distance_m"],
                                "amount": decision["amount"], "payment_requested_at": app_module.utc_now_iso(),
                                "payment_request_id": records.current_request_id(), "payment_attempts": 0,
                                "last_payment_outcome": None})
            if action == "settle_declined_from_ledger":
                reservation["last_payment_outcome"] = "decline"
                app_module.record_outstanding_balance(scooter)
            app_module.finish_ride(scooter)
            app_module.write_db(txn.db, txn.path)
            records.scooter(in_memory=scooter.to_dict())
            if action == "settle_declined_from_ledger":
                return _respond(200, dict(body, result="ended_with_balance", outstanding_balance=decision["amount"]))
            return _respond(200, dict(body, result="ended_paid"))
        # retry / settle_charged: one gateway attempt with the stored ride, like a rider's retry
        records.note("end", "retry", reason="reconcile", end_lat=reservation["end_lat"], end_lng=reservation["end_lng"],
                     amount=reservation["amount"])
        payment = app_module.pay(scooter)
        outcome = payment.outcome
        if payment.ok or outcome == "decline":
            amount = reservation["amount"]
            if outcome == "decline":
                app_module.record_outstanding_balance(scooter)
            app_module.finish_ride(scooter)
            app_module.write_db(txn.db, txn.path)
            records.scooter(in_memory=scooter.to_dict())
            if outcome == "decline":
                return _respond(200, dict(body, result="ended_with_balance", gateway_outcome=outcome, outstanding_balance=amount))
            return _respond(200, dict(body, result="ended_paid", gateway_outcome=outcome, txn_id=payment.txn_id))
        if outcome == "conflict":
            records.scooter(in_memory=scooter.to_dict())
            records.unchanged()
            return _report(scooter_id, "the gateway reports a charge for a different ride")
        app_module.note_payment_attempt(scooter, outcome)
        app_module.write_db(txn.db, txn.path)
        records.scooter(in_memory=scooter.to_dict())
        return _respond(202, dict(body, result="still_pending", gateway_outcome=outcome))


def make_app(now_fn, max_attempts, max_age):
    """A small internal Flask app (never served) whose one route runs one reconcile action,
    with the main app's request records and error handlers."""
    rc = Flask("reconcile")
    rc.config.update(RECONCILE_NOW=now_fn, RECONCILE_MAX_ATTEMPTS=max_attempts, RECONCILE_MAX_AGE=max_age)
    records.init_app(rc)
    rc.register_error_handler(StorageError, app_module.handle_storage_error)
    rc.register_error_handler(StorageBusy, app_module.handle_storage_busy)
    rc.register_error_handler(Exception, app_module.handle_unexpected_exception)
    rc.add_url_rule(PATH, "reconcile", _reconcile_one)
    return rc


# ---- a run --------------------------------------------------------------------------------

def _utcnow():
    return datetime.datetime.now(datetime.UTC)


def run(now=None, max_attempts=None, max_age=None, dry_run=False, only=None):
    """Reconcile every scooter that needs it (or just `only`) -> summary dict.

    now: a callable returning an aware datetime (injectable for tests); defaults to the clock.
    """
    now = now or _utcnow
    max_attempts = DEFAULT_MAX_ATTEMPTS if max_attempts is None else max_attempts
    max_age = max_age if max_age is not None else datetime.timedelta(minutes=DEFAULT_MAX_AGE_MINUTES)
    def look():                                                # a lock-free look
        db = app_module.init_db(app_module.get_db_path())
        return [(sid, d) for sid, d in plan(db, now(), max_attempts, max_age) if only is None or sid == only]

    run_id = uuid.uuid4().hex[:12]
    results = []
    if dry_run:     # one pass: what a later pass would do depends on what this one does
        results = [{"scooter_id": sid, **d, "result": "dry_run"} for sid, d in look()]
    else:
        client = make_app(now, max_attempts, max_age).test_client()
        finished = set()        # retried, reported or failed: not touched again this run
        for _ in range(MAX_PASSES):
            planned = [(sid, d) for sid, d in look() if sid not in finished]
            if not planned:
                break
            for sid, _ in planned:
                resp = client.get(PATH, query_string={"id": sid},
                                  headers={"X-Request-ID": f"reconcile-{run_id}-{len(results)}"})
                body = resp.get_json() or {}
                if "error" in body and "action" not in body:   # it failed (e.g. storage_error): nothing was done
                    body = {"action": "report", "result": "unchanged", "reason": body.get("msg"),
                            "error_kind": (body.get("error") or {}).get("kind")}
                result = dict(body, scooter_id=sid, status=resp.status_code, request_id=resp.headers.get("X-Request-ID"))
                results.append(result)
                if result.get("action") in FINAL_ACTIONS or result["status"] >= 400:
                    finished.add(sid)
    after = app_module.init_db(app_module.get_db_path())
    pending = [s.id for s in after if s.reservation and s.reservation.get("payment_state") == "payment_pending"]
    entries, problem = ledger_entries()
    paid_open = (None if problem else
                 sorted({s.id for s in after if paid_balances(s.to_dict(), entries)}))
    reported = [r for r in results if r.get("action") == "report" or r.get("status", 200) >= 500]
    return {"run_id": run_id, "dry_run": dry_run, "max_attempts": max_attempts,
            "max_age_minutes": max_age.total_seconds() / 60, "results": results,
            "still_pending": pending, "paid_balances_open": paid_open,
            "reported": sorted({r["scooter_id"] for r in reported})}


def remaining(summary):
    """True if the run leaves something for a human or a later run (exit 1)."""
    if summary["dry_run"]:
        return bool(summary["results"])
    # paid_balances_open is None when the ledger couldn't be read afterwards: not known clean
    return bool(summary["still_pending"] or summary["reported"] or summary.get("paid_balances_open", []) is None
                or summary.get("paid_balances_open"))


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(prog="python -m reconcile", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would be done; change nothing, write no records")
    parser.add_argument("--max-attempts", type=int,
                        help=f"gateway attempts before ending unpaid (default $RECONCILE_MAX_ATTEMPTS or {DEFAULT_MAX_ATTEMPTS})")
    parser.add_argument("--max-age-minutes", type=float,
                        help=f"minutes since payment started before ending unpaid (default $RECONCILE_MAX_AGE_MINUTES or {DEFAULT_MAX_AGE_MINUTES:g})")
    parser.add_argument("--only", metavar="SCOOTER_ID", help="reconcile just this scooter")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    max_attempts = args.max_attempts if args.max_attempts is not None else \
        _env_number("RECONCILE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, int)
    minutes = args.max_age_minutes if args.max_age_minutes is not None else \
        _env_number("RECONCILE_MAX_AGE_MINUTES", DEFAULT_MAX_AGE_MINUTES, float)
    if max_attempts < 1 or minutes < 0:
        parser.error("--max-attempts must be >= 1 and --max-age-minutes >= 0")
    try:
        summary = run(now=now, max_attempts=max_attempts, max_age=datetime.timedelta(minutes=minutes),
                      dry_run=args.dry_run, only=args.only)
    except StorageError as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary))
    else:
        verb = "would do" if args.dry_run else "did"
        print(f"reconcile {summary['run_id']}{' (dry run)' if args.dry_run else ''}: {len(summary['results'])} action(s); "
              f"limits {summary['max_attempts']} attempts / {summary['max_age_minutes']:g} min")
        for r in summary["results"]:
            print(f"  scooter {r['scooter_id']}: {verb} {r.get('action')} -> {r.get('result')}"
                  + (f" (txn {r['txn_id']})" if r.get("txn_id") else "")
                  + (f" balance {r['outstanding_balance']}" if r.get("outstanding_balance") is not None else "")
                  + f": {r.get('reason')}"
                  + (f"  [record {r['request_id']}]" if r.get("request_id") else ""))
        if not args.dry_run:
            paid_open = summary["paid_balances_open"]
            print(f"still payment_pending: {summary['still_pending'] or 'none'}; "
                  f"open balances the ledger shows paid: {'unknown (ledger unreadable)' if paid_open is None else paid_open or 'none'}; "
                  f"reported: {summary['reported'] or 'none'}")
    return 1 if remaining(summary) else 0


if __name__ == "__main__":
    sys.exit(main())
