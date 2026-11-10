"""The end-ride invariants (ACTIVITY Outcome 3, PLAN PR 14), checked across DB + ledger + records.

Given the DB as persisted, the gateway's ledger, and the request records, `violations()`
returns every broken invariant (empty list = all hold):

- I1 no charge without an ended ride: every charged idempotency key belongs to a ride that
  has ended, or that is payment_pending (a retry / reconcile will settle it). A charged key
  whose ride is `active` (or that also has an outstanding balance) is a violation.
- I2 no ended ride without a charge or an outstanding balance: every reservation that ever
  existed (from the records' successful starts plus the DB's initial reservations) and is no
  longer in the DB has exactly one charge or a recorded balance.
- I3 at most one charge per reservation (per idempotency key).
- I4 the scooter is never reserved without an active or payment_pending reservation, and
  never has a reservation while free.
"""
from collections import defaultdict

LIVE_STATES = {"active", "payment_pending"}


def _reservation(entry):
    if "reservation" in entry:
        return entry["reservation"]
    # legacy shape (no key): the app synthesises legacy-<id> for a reserved scooter
    return {"idempotency_key": f"legacy-{entry['id']}", "payment_state": "active"} if entry["is_reserved"] else None


def started_keys(records, initial_db):
    """Every reservation's idempotency key that ever existed: the initial DB's plus every
    successful start's."""
    keys = {r["idempotency_key"] for r in map(_reservation, initial_db) if r}
    for rec in records:
        if rec.get("path") == "/reservation/start" and rec.get("status") == 200:
            keys.add(rec["scooter_after"]["reservation"]["idempotency_key"])
    return keys


def violations(db, ledger, keys):
    found = []
    charges = defaultdict(list)
    for entry in ledger:
        if entry.get("charged"):
            charges[entry["idempotency_key"]].append(entry["txn_id"])
    for key, txns in charges.items():
        if len(txns) > 1:
            found.append(("I3 more than one charge", key, txns))
    live, balances = {}, defaultdict(list)
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
    for key in charges:
        if live.get(key) == "active":
            found.append(("I1 charged, but the ride is active (not ended, not pending)", key, None))
        if key in balances:
            found.append(("I1 charged, and also recorded as an outstanding balance", key, None))
    for key in keys:
        if key in live:
            continue                                   # still riding, or pending (settleable)
        if key not in charges and key not in balances:
            found.append(("I2 ended without a charge or an outstanding balance", key, None))
        if len(balances.get(key, [])) > 1:
            found.append(("I2 more than one outstanding balance", key, None))
    return found
