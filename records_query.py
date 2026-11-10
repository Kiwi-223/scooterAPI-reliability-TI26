"""Query request records (records.py) without reading raw JSONL.

    python -m records_query recent-failures [--since 1h] [--limit 20]
    python -m records_query failure-counts [--since 1h]
    python -m records_query scooter <id>
    python -m records_query request <request_id>
    python -m records_query anomalies [--since 1h]
    make records ARGS="failure-counts --since 1h"

Global options (before the command): --file PATH (default: $SCOOTER_RECORDS_PATH, else
data/records.jsonl) and --json (machine-readable output on stdout).

Exit codes: 0 = success; 1 = nothing found for `scooter`/`request`, or `anomalies` found at
least one; 2 = bad arguments or the records file is missing/unreadable.

The query functions are pure: they take an iterable of record dicts (see `read_records`)
and return plain data, so the MCP server (PLAN PR 16) reuses them as-is. Reading streams
the file line by line; a line that isn't a complete JSON object (e.g. a partially written
last line) is skipped with a warning instead of failing the query.
"""
import argparse
import datetime
import heapq
import json
import os
import re
import sys

DEFAULT_RECORDS_PATH = os.path.join("data", "records.jsonl")
RESERVATION_PATHS = ("/reservation/start", "/reservation/end")
_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


# ---- reading ----------------------------------------------------------------------------

def records_path():
    return os.environ.get("SCOOTER_RECORDS_PATH") or DEFAULT_RECORDS_PATH


def read_records(path, warn=None):
    """Yield record dicts from a JSONL file, streaming. Bad lines are skipped and reported
    as warn(line_number, reason). Raises OSError if the file can't be opened."""
    # utf-8-sig: a byte-order mark (e.g. from an editor) must not swallow the first record.
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="\n") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.rstrip("\n")
            if not text.strip():
                continue
            try:
                record = json.loads(text)
            except ValueError as exc:
                if warn:
                    complete = "" if line.endswith("\n") else " (last line, no newline: partially written?)"
                    warn(number, f"not valid JSON{complete}: {exc.msg}")
                continue
            if not isinstance(record, dict):
                if warn:
                    warn(number, f"not a JSON object ({type(record).__name__})")
                continue
            yield record


# ---- time windows ---------------------------------------------------------------------

def parse_ts(value):
    """A record's ts (or any ISO-8601 string) as an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def parse_since(value, now=None):
    """`--since`: a duration back from now (30s, 15m, 1h, 2d, 1h30m) or an ISO-8601 time
    (naive means UTC). Returns an aware UTC datetime; raises ValueError if neither."""
    now = now or datetime.datetime.now(datetime.UTC)
    match = _DURATION_RE.fullmatch(value.strip()) if value.strip() else None
    if match and any(match.groups()):
        d, h, m, s = (int(x or 0) for x in match.groups())
        try:
            return now - datetime.timedelta(days=d, hours=h, minutes=m, seconds=s)
        except OverflowError:
            raise ValueError(f"--since {value!r} reaches back further than a date can represent; "
                             "use an ISO-8601 time instead") from None
    parsed = parse_ts(value.strip())
    if parsed is None:
        raise ValueError(f"--since must be a duration like 30s, 15m, 1h, 2d, 1h30m or an ISO-8601 time, got {value!r}")
    return parsed


def in_window(record, since):
    """True if the record started at or after `since` (inclusive). No window: always True.
    A record without a parseable ts is outside every window."""
    if since is None:
        return True
    ts = parse_ts(record.get("ts"))
    return ts is not None and ts >= since


def _ts_key(record):
    return parse_ts(record.get("ts")) or datetime.datetime.min.replace(tzinfo=datetime.UTC)


def acquired_at(record):
    """When a mutating request got the DB lock (its `db_lock: acquired` decision), or None."""
    for d in record.get("decisions") or []:
        if isinstance(d, dict) and d.get("step") == "db_lock" and d.get("outcome") == "acquired":
            return parse_ts((d.get("detail") or {}).get("acquired_at"))
    return None


def _effect_key(record):
    """When a request took effect: its lock acquisition if it had one (PLAN PR 11c), else its ts.
    Requests that change a scooter run one at a time under the DB lock, in acquisition order,
    which can differ from the order they started in (ts)."""
    return acquired_at(record) or _ts_key(record)


# ---- queries (pure: iterable of records in, plain data out) ----------------------------

def recent_failures(records, since=None, limit=20):
    """Failed requests (error != null), newest first, at most `limit`."""
    failed = (r for r in records if isinstance(r.get("error"), dict) and in_window(r, since))
    return heapq.nlargest(limit, failed, key=_ts_key)


def failure_counts(records, since=None):
    """{error.kind: count} for failed requests in the window, most frequent first."""
    counts = {}
    for record in records:
        error = record.get("error")
        if isinstance(error, dict) and in_window(record, since):
            kind = error.get("kind") or "<no kind>"
            counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def scooter_history(records, scooter_id):
    """Every record about one scooter, in the order they took effect: by lock acquisition
    (`acquired_at`) where the record has one, else by ts. File order is completion order."""
    return sorted((r for r in records if r.get("scooter_id") == scooter_id), key=_effect_key)


def find_requests(records, request_id):
    """Records with this request_id (normally one; a reused client-supplied id gives more)."""
    return [r for r in records if r.get("request_id") == request_id]


def _float_pair(lat, lng):
    try:
        return (float(lat), float(lng))
    except (TypeError, ValueError):
        return None


def _requested_end(record):
    """Where an end request's ride ended, as the app decided it. Since PLAN PR 14 that's the
    location stored when payment started (the payment `requested` decision's end_lat/end_lng):
    a retry of a payment_pending ride ends there, whatever the retry's own params say. Older
    records fall back to the request's lat/lng params (first value of each)."""
    for d in record.get("decisions") or []:
        if isinstance(d, dict) and d.get("step") == "payment" and d.get("outcome") == "requested":
            detail = d.get("detail") or {}
            if "end_lat" in detail and "end_lng" in detail:
                return _float_pair(detail["end_lat"], detail["end_lng"])
    params = record.get("params")
    if not isinstance(params, dict):
        return None
    first = lambda v: v[0] if isinstance(v, list) and v else v
    return _float_pair(first(params.get("lat")), first(params.get("lng")))


def _decision(decisions, step, outcome):
    """(index, detail) of the first decision with this step and outcome, or (None, None)."""
    for n, d in enumerate(decisions):
        if d.get("step") == step and d.get("outcome") == outcome:
            return n, d.get("detail") or {}
    return None, None


def _final_payment(decisions):
    """The last payment result (outcome ok/failed) and its detail, or (None, None)."""
    for d in reversed(decisions):
        if d.get("step") == "payment" and d.get("outcome") in ("ok", "failed"):
            return d.get("outcome"), d.get("detail") or {}
    return None, None


def _ride_values(detail, amount_key):
    """(end_lat, end_lng, amount) from a decision detail, as floats where possible."""
    pair = _float_pair(detail.get("end_lat"), detail.get("end_lng"))
    return (pair, detail.get(amount_key)) if pair is not None else None


def _finding(r, kind, detail):
    return {"type": kind, "request_id": r.get("request_id"), "ts": r.get("ts"), "path": r.get("path"),
            "scooter_id": r.get("scooter_id"), "status": r.get("status"), "detail": detail}


def anomalies(records, since=None):
    """Records whose own fields contradict each other. Each finding is a dict with
    `type`, `request_id`, `ts`, `path`, `scooter_id`, `status` and `detail`.

    - write_not_persisted: db_write.ok but scooter_after != scooter_in_memory (the code
      changed the scooter, but what was written doesn't contain that change)
    - success_without_write: a /reservation/* request returned 200 without attempting a write
    - end_left_reserved: /reservation/end returned 200 but scooter_after is still reserved
    - start_left_free: /reservation/start returned 200 but scooter_after isn't reserved
    - end_location_not_persisted: /reservation/end returned 200 but scooter_after's lat/lng
      differ from where the ride ended (the stored end location since PLAN PR 14, else the
      requested coordinates; compared as floats)
    - reservation_flag_mismatch: scooter_after breaks the DB invariant "is_reserved is true
      exactly when reservation is set" (PLAN PR 12)
    - start_reservation_not_persisted: /reservation/start returned 200 but scooter_after has
      no reservation created by this request (its start_request_id isn't this request_id)

    - charged_without_persisted_end: a payment came back ok (the ride is paid) but a later DB
      write in the same request failed or was refused, so the ride is still payment_pending
      on disk: a retry or reconcile settles it (PLAN PR 14)
    - decline_not_settled: payment_declined, but scooter_after is still reserved or has no
      outstanding balance for the reservation (H3: a decline ends the ride with a balance)
    - failed_payment_not_pending: payment_timeout / payment_failed, but scooter_after's
      reservation isn't payment_pending (the ride must stay settleable)
    - ended_without_payment: /reservation/end returned 200 but the request's final payment
      result isn't ok (or there was none); or payment_declined whose final gateway outcome
      isn't a decline
    - charged_before_pending_persisted: a first-attempt end (it has a `payment: pending`
      decision) asked the gateway to charge before any `db_write: ok` after that decision.
      Phase 1 must be on disk before the charge (PLAN PR 14)
    - pending_location_changed: a retry (`end: retry`) re-priced the ride (it has its own
      `payment: pending`), or charged a different end location or amount than the one
      stored when payment started (this record's retry decision, or any earlier record's
      `payment: pending` for the same idempotency key)

    - payment_key_mismatch: the payment result says it was charged under an idempotency
      key (or reservation) other than the reservation's own (scooter_before)
    - double_charge: a second charge (a different ledger txn) for a reservation that a
      record already shows charged, i.e. one ride paid twice. Checked across records.
    - scooter_double_charge: two charges on one scooter between a successful start and the
      scooter becoming free, under any reservation or key (PLAN PR 15). Checked across
      records, in the order they took effect.
    - reconcile_not_settled: a /reconcile record (PLAN PR 15) says it ended the ride (status
      200, an action other than none) but scooter_after is still reserved; or it ended it
      unpaid without an outstanding balance for that reservation.

    decline_not_settled also requires the balance's amount to equal the amount charged for
    (the `payment: requested` cost) and its idempotency_key to be the reservation's.

    The reservation rules only look at snapshots that have a "reservation" key: records
    written before PLAN PR 12 have none, and aren't flagged for it.

    Deliberately not included: a cross-record continuity check (a scooter's scooter_before
    should equal its previous scooter_after). Concurrent workers, `git restore` of the DB
    and manual edits all break that chain legitimately, so it would mostly be noise; the
    per-record rules above catch persistence bugs inside the request that caused them.
    """
    found = []
    pending_by_key = {}     # idempotency key -> {(location, amount)} fixed by phase 1
    retries = []            # (key, values, flag) checked against pending_by_key at the end
    charged_by_reservation = {}   # reservation_id -> the first txn a record shows charged for it
    scooter_events = []           # (effect time, scooter, started, charged, freed, record, in window)
    for r in records:
        decisions = [d for d in r.get("decisions") or [] if isinstance(d, dict)]
        pending_at, pending = _decision(decisions, "payment", "pending")
        requested_at, requested = _decision(decisions, "payment", "requested")
        key = (requested or {}).get("idempotency_key")
        if pending_at is not None and key is not None:
            # every record, in or out of the window, can supply the stored values
            values = _ride_values(pending, "amount")
            if values is not None:
                pending_by_key.setdefault(key, set()).add(values)
        # charges are tracked across the whole file, so an earlier charge outside --since counts
        _, paid_detail = _final_payment(decisions)
        simulator = (paid_detail or {}).get("simulator") or {}
        charged_rid = (paid_detail or {}).get("reservation_id") or \
            ((r.get("scooter_before") or {}).get("reservation") or {}).get("reservation_id")
        double = None
        if simulator.get("charged") is True and charged_rid is not None:
            txn = simulator.get("ledger_txn_id")
            first = charged_by_reservation.setdefault(charged_rid, txn)
            if first != txn:
                double = {"reservation_id": charged_rid, "earlier_txn_id": first, "this_txn_id": txn,
                          "charged_under": paid_detail.get("idempotency_key")}
        after_early = r.get("scooter_after")
        if r.get("scooter_id") is not None:
            started = r.get("path") == "/reservation/start" and r.get("status") == 200
            freed = isinstance(after_early, dict) and after_early.get("is_reserved") is False and \
                (r.get("scooter_before") or {}).get("is_reserved") is True
            if started or freed or simulator.get("charged") is True:
                scooter_events.append((_effect_key(r), r.get("scooter_id"), started, simulator.get("charged") is True,
                                       freed, r, in_window(r, since)))
        if not in_window(r, since):
            continue
        db_write = r.get("db_write") or {}
        after, in_memory = r.get("scooter_after"), r.get("scooter_in_memory")
        status, path = r.get("status"), r.get("path")

        def flag(kind, detail, r=r):
            found.append(_finding(r, kind, detail))

        if db_write.get("ok") is True and after is not None and in_memory is not None and after != in_memory:
            flag("write_not_persisted", {"scooter_after": after, "scooter_in_memory": in_memory})
        # NOTE for PLAN PR 14: an idempotent retry of /reservation/end (same idempotency key,
        # already settled) may legitimately return 200 without writing. That PR must exempt
        # such retries here (e.g. via a decision it records) or this rule will flag them.
        if path in RESERVATION_PATHS and status == 200 and not db_write.get("attempted"):
            flag("success_without_write", {"db_write": db_write})
        if path == "/reservation/end" and status == 200 and isinstance(after, dict) and after.get("is_reserved") is True:
            flag("end_left_reserved", {"scooter_after": after})
        if path == "/reservation/start" and status == 200 and isinstance(after, dict) and after.get("is_reserved") is not True:
            flag("start_left_free", {"scooter_after": after})
        if path == "/reservation/end" and status == 200 and isinstance(after, dict):
            wanted = _requested_end(r)
            got = _float_pair(after.get("lat"), after.get("lng"))
            if wanted is not None and got != wanted:
                flag("end_location_not_persisted", {"requested": {"lat": wanted[0], "lng": wanted[1]},
                                                    "scooter_after": after})
        paid_at = next((n for n, d in enumerate(decisions) if d.get("step") == "payment" and d.get("outcome") == "ok"), None)
        if paid_at is not None:
            lost = [d for d in decisions[paid_at + 1:] if d.get("step") == "db_write" and d.get("outcome") in ("failed", "refused")]
            if lost:
                flag("charged_without_persisted_end", {"payment": decisions[paid_at].get("detail"), "db_write": lost[0]})
        kind = (r.get("error") or {}).get("kind")
        if kind == "payment_declined" and isinstance(after, dict) and "reservation" in after:
            before_res = (r.get("scooter_before") or {}).get("reservation") or {}
            balances = after.get("outstanding_balances") or []
            charged_for = (requested or {}).get("cost")
            if after.get("reservation") is not None or not any(
                    isinstance(b, dict) and b.get("reservation_id") == before_res.get("reservation_id")
                    and b.get("idempotency_key") == before_res.get("idempotency_key")
                    and (charged_for is None or b.get("amount") == charged_for) for b in balances):
                flag("decline_not_settled", {"scooter_after": after, "amount_charged_for": charged_for})
        final, final_detail = _final_payment(decisions)
        before_res = (r.get("scooter_before") or {}).get("reservation") or {}
        if final is not None and before_res:
            for field in ("idempotency_key", "reservation_id"):
                if field in final_detail and final_detail[field] != before_res.get(field):
                    flag("payment_key_mismatch", {"field": field, "charged_under": final_detail[field],
                                                  "reservation": before_res.get(field)})
                    break
        if double is not None:
            flag("double_charge", double)
        # (a record with no decisions at all can't say how it paid, so it isn't judged)
        if path == "/reservation/end" and status == 200 and decisions and final != "ok":
            flag("ended_without_payment", {"final_payment": final, "gateway_outcome": (final_detail or {}).get("gateway_outcome")})
        if kind == "payment_declined" and final is not None and (final_detail or {}).get("gateway_outcome", "decline") != "decline":
            flag("ended_without_payment", {"final_payment": final, "gateway_outcome": final_detail.get("gateway_outcome")})
        if pending_at is not None and requested_at is not None:
            persisted = any(d.get("step") == "db_write" and d.get("outcome") == "ok"
                            for d in decisions[pending_at + 1:requested_at])
            if not persisted:
                flag("charged_before_pending_persisted", {"payment_pending": pending, "payment_requested": requested})
        retry_at, retry = _decision(decisions, "end", "retry")
        if retry_at is not None and requested_at is not None:
            charged = _ride_values(requested, "cost")
            stored = _ride_values(retry, "amount")
            if pending_at is not None or (stored is not None and charged != stored):
                flag("pending_location_changed", {"stored": retry, "payment_requested": requested})
            elif key is not None and charged is not None:
                retries.append((key, charged, r))
        if kind in ("payment_timeout", "payment_failed") and isinstance(after, dict) and "reservation" in after:
            if (after.get("reservation") or {}).get("payment_state") != "payment_pending":
                flag("failed_payment_not_pending", {"scooter_after": after})
        if path == "/reconcile":
            _, rc = _decision(decisions, "reconcile", "end_unpaid")
            acted = [d for d in decisions if d.get("step") == "reconcile" and d.get("outcome") not in ("none", "report", "skipped")]
            if status == 200 and acted and isinstance(after, dict) and after.get("is_reserved") is not False:
                flag("reconcile_not_settled", {"reconcile": acted[0], "scooter_after": after})
            elif rc is not None and isinstance(after, dict):
                rid = before_res.get("reservation_id")
                if not any(isinstance(b, dict) and b.get("reservation_id") == rid and b.get("reason") == "payment_unresolved"
                           for b in after.get("outstanding_balances") or []):
                    flag("reconcile_not_settled", {"reconcile": rc, "scooter_after": after,
                                                   "problem": "ended unpaid without an outstanding balance"})
        if isinstance(after, dict) and "reservation" in after:
            reservation = after["reservation"]
            if (after.get("is_reserved") is True) != (reservation is not None):
                flag("reservation_flag_mismatch", {"scooter_after": after})
            if path == "/reservation/start" and status == 200 and (
                    not isinstance(reservation, dict) or reservation.get("start_request_id") != r.get("request_id")):
                flag("start_reservation_not_persisted", {"scooter_after": after})
    charges_since_start = {}
    for _, scooter, started, charged, freed, r, in_win in sorted(scooter_events, key=lambda e: e[0]):
        if started:
            charges_since_start[scooter] = 0
        if charged:
            charges_since_start[scooter] = charges_since_start.get(scooter, 0) + 1
            if charges_since_start[scooter] > 1 and in_win:
                found.append(_finding(r, "scooter_double_charge", {"charges_since_start": charges_since_start[scooter]}))
        if freed:
            charges_since_start[scooter] = 0
    for key, charged, r in retries:
        known = pending_by_key.get(key)
        if known and charged not in known:
            found.append(_finding(r, "pending_location_changed", {"charged": {"lat": charged[0][0], "lng": charged[0][1], "amount": charged[1]},
                                              "stored_for_key": sorted([v[0][0], v[0][1], v[1]] for v in known)}))
    return sorted(found, key=_ts_key)


def ledger_check(ledger_entries, records, now=None, min_age_s=5):
    """Cross-check the payments ledger against the request records -> dict of lists:

    - open_orphans: ledger entries whose request_id has no request record (the request that
      called the gateway never finished, e.g. its worker was SIGKILLed), and no later record
      touched that idempotency key (no retry has settled it yet). Entries younger than
      min_age_s are skipped: their request may still be running.
    - resolved_orphans: the same, but a later record (a retry) used that key.
    - double_charges: two or more charges for one reservation_id, under any keys: one ride
      paid twice.
    - foreign_keys: a charge whose idempotency_key isn't its reservation_id. Every key equals
      its reservation_id (AGENTS.md "Database schema"), so this is a charge under a key no
      reservation owns.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    seen, key_uses = set(), {}
    for r in records:
        seen.add(r.get("request_id"))
        for d in r.get("decisions") or []:
            key = (d.get("detail") or {}).get("idempotency_key") if isinstance(d, dict) and d.get("step") == "payment" else None
            if key is not None:
                key_uses.setdefault(key, []).append(r.get("ts") or "")
    result = {"open_orphans": [], "resolved_orphans": [], "double_charges": [], "foreign_keys": []}
    charges = {}
    for entry in ledger_entries:
        rid, key = entry.get("request_id"), entry.get("idempotency_key")
        if entry.get("charged") is True:
            charges.setdefault(entry.get("reservation_id"), []).append(entry)
            if key != entry.get("reservation_id"):
                result["foreign_keys"].append(entry)
        if rid is None or rid in seen:
            continue
        ts = parse_ts(entry.get("ts"))
        if ts is not None and (now - ts).total_seconds() < min_age_s:
            continue
        later = [t for t in key_uses.get(key, []) if t >= (entry.get("ts") or "")]
        result["resolved_orphans" if later else "open_orphans"].append(entry)
    for reservation_id, entries in charges.items():
        if len(entries) > 1:
            result["double_charges"].append({"reservation_id": reservation_id, "charges": [
                {k: e.get(k) for k in ("ts", "idempotency_key", "txn_id", "amount", "request_id")} for e in entries]})
    return result


def ledger_problems(result):
    """True if ledger_check found something that needs attention (exit 1)."""
    return bool(result["open_orphans"] or result["double_charges"] or result["foreign_keys"])


def read_ledger(path, warn=None):
    """The payments ledger's entries (unreadable lines are skipped with a warning; the
    gateway's own `make payments ARGS="verify"` is the authority on ledger health)."""
    entries = []
    with open(path, "r", encoding="utf-8") as ledger:
        for n, line in enumerate(ledger, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if isinstance(entry, dict):
                entries.append(entry)
            elif warn:
                warn(n, "not a JSON object")
    return entries


# ---- CLI ------------------------------------------------------------------------------

def _summary(record):
    error = record.get("error") or {}
    return (f"{record.get('ts', '?'):24} {record.get('status', '?')!s:>3} "
            f"{(error.get('kind') or 'ok'):25} {record.get('method', '?'):4} {record.get('path', '?')} "
            f"id={record.get('request_id')} scooter={record.get('scooter_id')}")


def _payment(record):
    """The last payment result in a record: the gateway's outcome (PLAN PR 13+), or the older
    records' ok/failed, or None if the request didn't pay."""
    paid = [d for d in record.get("decisions") or [] if isinstance(d, dict) and d.get("step") == "payment"
            and d.get("outcome") in ("ok", "failed")]
    if not paid:
        return None
    detail = paid[-1].get("detail") or {}
    return detail.get("gateway_outcome") or paid[-1].get("outcome")


def _state(s):
    if not isinstance(s, dict):
        return "-"
    return f"{'reserved' if s.get('is_reserved') else 'free'}@({s.get('lat')},{s.get('lng')})"


def _print_human(command, result, args):
    window = f" since {args.since_dt.isoformat(timespec='seconds')}" if getattr(args, "since_dt", None) else ""
    if command == "recent-failures":
        print(f"{len(result)} most recent failure(s){window}, newest first:")
        for record in result:
            print("  " + _summary(record))
    elif command == "failure-counts":
        print(f"failures by kind{window}: {sum(result.values())} total")
        for kind, count in result.items():
            print(f"  {count:6} {kind}")
    elif command == "scooter":
        print(f"scooter {args.scooter_id}: {len(result)} record(s), oldest first")
        for r in result:
            payment = _payment(r)
            print(f"  {_summary(r)}\n      before={_state(r.get('scooter_before'))} "
                  f"after={_state(r.get('scooter_after'))} in_memory={_state(r.get('scooter_in_memory'))}"
                  + (f" payment={payment}" if payment else ""))
    elif command == "request":
        for record in result:
            print(json.dumps(record, indent=2, ensure_ascii=False))
    elif command == "ledger-check":
        def entry_line(e):
            return (f"    {e.get('ts')} key={e.get('idempotency_key')} attempt={e.get('attempt')} outcome={e.get('outcome')} "
                    f"charged={e.get('charged')} request_id={e.get('request_id')} scooter={e.get('scooter_id')}")
        print(f"double charges: {len(result['double_charges'])}")
        for d in result["double_charges"]:
            print(f"    reservation {d['reservation_id']} charged {len(d['charges'])} times: "
                  + ", ".join(f"{c['txn_id']} (key {c['idempotency_key']}, {c['amount']})" for c in d["charges"]))
        print(f"charges under a key that isn't their reservation's: {len(result['foreign_keys'])}")
        for e in result["foreign_keys"]:
            print(entry_line(e))
        print(f"open orphans (the request died mid-payment and nothing has settled its key since): {len(result['open_orphans'])}")
        for e in result["open_orphans"]:
            print(entry_line(e))
        if result["open_orphans"]:
            print("    Its ride is usually still payment_pending: retry /reservation/end (or reconcile) to settle it\n"
                  "    with this charge; check with make payments ARGS=\"lookup <key>\" and the DB.")
        print(f"resolved orphans (a later request used the key): {len(result['resolved_orphans'])}")
        for e in result["resolved_orphans"]:
            print(entry_line(e))
    elif command == "anomalies":
        print(f"{len(result)} anomaly(ies){window}")
        for a in result:
            print(f"  {a['type']:22} {a['ts']} {a['path']} id={a['request_id']} scooter={a['scooter_id']}\n"
                  f"      {json.dumps(a['detail'])}")


def build_parser():
    parser = argparse.ArgumentParser(prog="records_query", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", help="records file (default: $SCOOTER_RECORDS_PATH or data/records.jsonl)")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("recent-failures", help="newest failed requests")
    p.add_argument("--since", help="duration (30s, 15m, 1h, 2d, 1h30m) or ISO-8601 time")
    p.add_argument("--limit", type=int, default=20, help="max rows (default 20)")
    p = sub.add_parser("failure-counts", help="number of failures per error.kind")
    p.add_argument("--since", help="duration (30s, 15m, 1h, 2d, 1h30m) or ISO-8601 time")
    p = sub.add_parser("scooter", help="one scooter's history, oldest first")
    p.add_argument("scooter_id")
    p = sub.add_parser("request", help="the full record(s) for one request id")
    p.add_argument("request_id")
    p = sub.add_parser("anomalies", help="records whose fields contradict each other (exit 1 if any)")
    p.add_argument("--since", help="duration (30s, 15m, 1h, 2d, 1h30m) or ISO-8601 time")
    p = sub.add_parser("ledger-check",
                       help="cross-check the payments ledger with the records: requests that died mid-payment, "
                            "double charges, charges under foreign keys (exit 1 if any)")
    p.add_argument("--ledger", help="ledger path (default: $SCOOTER_PAYMENTS_LEDGER_PATH or data/payments_ledger.jsonl)")
    p.add_argument("--min-age", type=float, default=5.0,
                   help="ignore entries younger than this many seconds (their request may still be running; default 5)")
    return parser


def main(argv=None, now=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.since_dt = None
    if getattr(args, "since", None) is not None:
        try:
            args.since_dt = parse_since(args.since, now)
        except ValueError as exc:
            parser.error(str(exc))                       # exits 2
    if getattr(args, "limit", 1) < 1:
        parser.error("--limit must be at least 1")
    path = args.file or records_path()
    warnings = []
    records = read_records(path, warn=lambda n, reason: warnings.append((n, reason)))
    try:
        if args.command == "recent-failures":
            result = recent_failures(records, args.since_dt, args.limit)
        elif args.command == "failure-counts":
            result = failure_counts(records, args.since_dt)
        elif args.command == "scooter":
            result = scooter_history(records, args.scooter_id)
        elif args.command == "request":
            result = find_requests(records, args.request_id)
        elif args.command == "ledger-check":
            ledger = args.ledger or os.environ.get("SCOOTER_PAYMENTS_LEDGER_PATH") or os.path.join("data", "payments_ledger.jsonl")
            try:
                entries = read_ledger(ledger, warn=lambda n, reason: warnings.append((n, f"{reason} (in {ledger})")))
            except OSError as exc:
                print(f"records_query: cannot read ledger {ledger!r}: {exc.strerror or exc}", file=sys.stderr)
                return 2
            result = ledger_check(entries, records, now=now, min_age_s=args.min_age)
        else:
            result = anomalies(records, args.since_dt)
    except OSError as exc:
        print(f"records_query: cannot read records file {path!r}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    for number, reason in warnings:
        print(f"records_query: warning: {path}:{number}: skipped line, {reason}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        _print_human(args.command, result, args)

    if args.command in ("scooter", "request") and not result:
        wanted = args.scooter_id if args.command == "scooter" else args.request_id
        print(f"records_query: no records for {args.command} {wanted!r} in {path}", file=sys.stderr)
        return 1
    if args.command == "anomalies" and result:
        return 1
    if args.command == "ledger-check" and ledger_problems(result):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
