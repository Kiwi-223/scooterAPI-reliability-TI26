"""The end-ride invariants (ACTIVITY Outcome 3, PLAN PR 14), checked across DB + ledger + records.

Given the DB as persisted, the gateway's ledger, and the request records, `violations()`
returns every broken invariant (empty list = all hold):

- I1 no charge without an ended ride: every charged idempotency key belongs to a ride that
  has ended, or that is payment_pending (a retry / reconcile will settle it). A charged key
  whose ride is `active` (or that also has an open outstanding balance) is a violation. A
  balance marked settled (`settled_by_txn_id`, reconcile's clear_paid_balance, PLAN PR 15b)
  is not owed, but its txn must be a ledger charge for that same reservation.
- I2 no ended ride without a charge or an outstanding balance: every reservation that ever
  existed (from the records' successful starts plus the DB's initial reservations) and is no
  longer in the DB has exactly one charge or a recorded balance (at most one open).
- I3 at most one charge per reservation: per idempotency key, and per reservation_id (the
  ledger records both, so a bug that charges one ride under two keys is caught), and no
  charge under a key that no reservation ever owned (`keys`).
- I4 the scooter is never reserved without an active or payment_pending reservation, and
  never has a reservation while free.
- I5 every charge (the ledger's `amount` for a charged key) and every outstanding balance's
  `amount` equals an amount fixed when that ride's payment started: the records' `payment:
  requested` cost for that key. (Several only after a restore + conflict.) Checked only
  when `amounts` is given and has the key: I1-I4 check presence, I5 checks the amount.
"""
from collections import defaultdict

LIVE_STATES = {"active", "payment_pending"}


def _reservation(entry):
    if "reservation" in entry:
        return entry["reservation"]
    # legacy shape (no key): the app synthesises legacy-<id> for a reserved scooter
    return {"idempotency_key": f"legacy-{entry['id']}", "payment_state": "active"} if entry["is_reserved"] else None


def started_keys(records, initial_db):
    """Every reservation's idempotency key that ever existed: the initial DB's, every
    successful start's, and every reservation any record saw (its `scooter_before` or
    `scooter_after`). The last covers rides older than the records, e.g. the committed DB's
    legacy-33, when the current DB stands in for the initial one (the CLI below, the MCP
    `check_invariants`): otherwise a ride that started before the records and has ended
    since looks like a charge under a key nobody owned (PR #24 review)."""
    keys = {r["idempotency_key"] for r in map(_reservation, initial_db) if r}
    for rec in records:
        if rec.get("path") == "/reservation/start" and rec.get("status") == 200:
            keys.add(rec["scooter_after"]["reservation"]["idempotency_key"])
        for snapshot in (rec.get("scooter_before"), rec.get("scooter_after")):
            reservation = snapshot.get("reservation") if isinstance(snapshot, dict) else None
            if isinstance(reservation, dict) and reservation.get("idempotency_key"):
                keys.add(reservation["idempotency_key"])
    return keys


def pending_amounts(records):
    """{idempotency key: {amounts fixed at pending}} from the records' payment `requested`
    decisions (the cost the gateway was asked for, which is the stored amount)."""
    amounts = defaultdict(set)
    for rec in records:
        for d in rec.get("decisions") or []:
            if d.get("step") == "payment" and d.get("outcome") == "requested":
                detail = d.get("detail") or {}
                if detail.get("idempotency_key") is not None:
                    amounts[detail["idempotency_key"]].add(detail.get("cost"))
    return amounts


def violations(db, ledger, keys, amounts=None):
    found = []
    charges = defaultdict(list)
    for entry in ledger:
        if entry.get("charged"):
            charges[entry["idempotency_key"]].append(entry["txn_id"])
    for key, txns in charges.items():
        if len(txns) > 1:
            found.append(("I3 more than one charge", key, txns))
        if key not in keys:
            found.append(("I3 charged under a key no reservation owned", key, txns))
    by_reservation = defaultdict(list)
    for entry in ledger:
        if entry.get("charged") and entry.get("reservation_id") is not None:
            by_reservation[entry["reservation_id"]].append(entry["txn_id"])
    for reservation_id, txns in by_reservation.items():
        if len(txns) > 1:
            found.append(("I3 one reservation charged more than once (a double charge)", reservation_id, txns))
    txn_reservation = {e.get("txn_id"): e.get("reservation_id") for e in ledger if e.get("charged")}
    live, balances, open_balances = {}, defaultdict(list), defaultdict(list)
    for entry in db:
        reservation = _reservation(entry)
        if entry["is_reserved"] != (reservation is not None):
            found.append(("I4 is_reserved disagrees with reservation", entry["id"], reservation))
        if reservation is not None:
            if reservation.get("payment_state") not in LIVE_STATES:
                found.append(("I4 reserved without an active/pending reservation", entry["id"], reservation))
            live[reservation["idempotency_key"]] = reservation.get("payment_state")
        for balance in entry.get("outstanding_balances") or []:
            balances[balance["idempotency_key"]].append(balance)
            if "settled_by_txn_id" not in balance:
                open_balances[balance["idempotency_key"]].append(balance)
            elif txn_reservation.get(balance["settled_by_txn_id"]) != balance.get("reservation_id"):
                found.append(("I1 balance settled by a txn that isn't a charge for its reservation",
                              balance.get("reservation_id"), balance["settled_by_txn_id"]))
    for key in charges:
        if live.get(key) == "active":
            found.append(("I1 charged, but the ride is active (not ended, not pending)", key, None))
        if key in open_balances:
            found.append(("I1 charged, and also recorded as an outstanding balance", key, None))
    # the same per RESERVATION (business identity), whatever keys the charge and the balance
    # were made under (PR #20 review: a key switch hid "charged and owes" from the per-key check)
    owed_reservations = {b.get("reservation_id") for owed in open_balances.values() for b in owed}
    for reservation_id in by_reservation:
        if reservation_id in owed_reservations:
            found.append(("I1 a reservation was charged and also owes a balance", reservation_id, None))
    for entry in ledger:
        key = entry["idempotency_key"]
        if amounts and key in amounts and entry.get("charged") and entry.get("amount") not in amounts[key]:
            found.append(("I5 charged an amount that wasn't fixed at pending", key, entry.get("amount")))
    for key, owed in balances.items():
        for balance in owed:
            if amounts and key in amounts and balance.get("amount") not in amounts[key]:
                found.append(("I5 balance amount differs from the amount fixed at pending", key, balance.get("amount")))
    for key in keys:
        if key in live:
            continue                                   # still riding, or pending (settleable)
        if key not in charges and key not in balances:
            found.append(("I2 ended without a charge or an outstanding balance", key, None))
        if len(open_balances.get(key, [])) > 1:
            found.append(("I2 more than one outstanding balance", key, None))
    return found


def main(argv=None):
    """`.venv/bin/python tests/invariants.py`: check I1-I5 on the current DB, ledger and records
    (PLAN PR 15b; the way to confirm a restored DB, which the records can't see). Paths come
    from SCOOTER_DB_PATH / SCOOTER_PAYMENTS_LEDGER_PATH / SCOOTER_RECORDS_PATH as the app uses
    them; a missing ledger or records file counts as empty. Read-only. Exit 0 = all hold."""
    import json
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import payments
    import records_query as rq
    db_path = os.environ.get("SCOOTER_DB_PATH") or "scooter_db.json"
    with open(db_path, encoding="utf-8") as f:
        db = json.load(f)
    ledger = payments.all_entries() if os.path.exists(payments.ledger_path()) else []
    records = list(rq.read_records(rq.records_path())) if os.path.exists(rq.records_path()) else []
    # the current DB stands in for the initial one: a ride from before the records began
    # counts only while it's still in the DB
    found = violations(db, ledger, started_keys(records, db), pending_amounts(records))
    for v in found:
        print(json.dumps(v))
    print(f"{len(found)} invariant violation(s) in {db_path} ({len(ledger)} ledger entries, {len(records)} records)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
