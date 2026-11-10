"""Simulated payment gateway with a persisted ledger and reproducible faults (PLAN PR 13).

`charge(...)` is the only call the app makes. Every call appends one line to the ledger:

    SCOOTER_PAYMENTS_LEDGER_PATH  (default: data/payments_ledger.jsonl, relative to the cwd)

The ledger is the gateway's own memory. It is shared by every worker and survives restarts,
and lines are appended under an exclusive `fcntl.flock`, so:

- **At most one charge per idempotency key.** Under the lock, a key that already has a
  charge is never charged again. A hit counts as "already charged" (the old `txn_id` is
  returned) only if it is the same ride: same scooter id, reservation id, end location
  (exact, as the app parsed it) and amount. Anything else is a `conflict`, never a free
  ride and never a second charge. (The amount alone is a rounded distance, so two
  different end points at the same distance would look alike: PR #16's review.)
- **Attempt numbers** are counted from the ledger (1 + earlier lines for the key), so they
  don't depend on which worker served the request.

Outcomes: `success`, `decline`, `timeout_not_charged`, `timeout_charged`, `error`, plus the
ledger's own `already_charged` and `conflict`. Which fault happens is chosen, in order of
precedence, by:

1. the ledger: a key that already has a charge gives `already_charged` or `conflict`;
2. the `X-Payment-Fault` request header, honoured only when ENABLE_FAULT_INJECTION=1;
3. the PAYMENT_FAULT env var: `none` (the default), an outcome name to force it on every
   attempt, or `random`: a seeded draw that is a pure function of (PAYMENT_SEED,
   idempotency key, attempt), with a fault probability of PAYMENT_FAULT_RATE.

Every result says where its outcome came from (`fault`), with the draw's inputs, so a
failure can be replayed exactly: `python -m payments draw --seed S --key K --attempt N`.
`python -m payments verify` checks that the ledger is readable (AGENTS.md has the runbook).
AGENTS.md "Payments & fault injection" has the recipes.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field

DEFAULT_LEDGER_PATH = os.path.join("data", "payments_ledger.jsonl")
FAULT_HEADER = "X-Payment-Fault"

SUCCESS = "success"
FAULTS = ("decline", "timeout_not_charged", "timeout_charged", "error")
OUTCOMES = (SUCCESS,) + FAULTS                    # what the simulated card network can do
LEDGER_OUTCOMES = ("already_charged", "conflict")  # what the ledger itself decides
CHARGING_OUTCOMES = {SUCCESS, "timeout_charged"}  # outcomes that take the rider's money
TIMEOUTS = {"timeout_not_charged", "timeout_charged"}
# Accepted by PAYMENT_FAULT and the header: `none`/`success` (no fault), a fault, `random`.
FAULT_MODES = ("none", SUCCESS) + FAULTS + ("random",)

DEFAULT_SEED = "0"
# A key that was declined stays declined for the same ride (PLAN PR 15b). Only tests turn this
# off, to reproduce ledgers written before 15b (a restore could replay a declined ride into a
# charge).
REPLAY_DECLINES = True
DEFAULT_FAULT_RATE = 0.5
DEFAULT_TIMEOUT_DELAY_MS = 20


class PaymentConfigError(ValueError):
    """PAYMENT_FAULT / PAYMENT_FAULT_RATE / PAYMENT_TIMEOUT_DELAY_MS has an invalid value."""


# ---- configuration (read on every call, like the app's other env vars) -------------------

def ledger_path():
    return os.environ.get("SCOOTER_PAYMENTS_LEDGER_PATH") or DEFAULT_LEDGER_PATH


def fault_injection_enabled():
    return os.environ.get("ENABLE_FAULT_INJECTION") == "1"


def _fault_mode():
    mode = (os.environ.get("PAYMENT_FAULT") or "none").strip()
    if mode not in FAULT_MODES:
        raise PaymentConfigError(f"PAYMENT_FAULT={mode!r} is not one of {list(FAULT_MODES)}")
    return mode


def _seed():
    return os.environ.get("PAYMENT_SEED") or DEFAULT_SEED


def _fault_rate():
    raw = os.environ.get("PAYMENT_FAULT_RATE")
    if not raw:
        return DEFAULT_FAULT_RATE
    try:
        rate = float(raw)
    except ValueError:
        rate = float("nan")
    if not 0 <= rate <= 1:
        raise PaymentConfigError(f"PAYMENT_FAULT_RATE={raw!r} is not a number in [0, 1]")
    return rate


def _timeout_delay_s():
    raw = os.environ.get("PAYMENT_TIMEOUT_DELAY_MS")
    try:
        delay = float(raw) if raw else DEFAULT_TIMEOUT_DELAY_MS
    except ValueError:
        raise PaymentConfigError(f"PAYMENT_TIMEOUT_DELAY_MS={raw!r} is not a number") from None
    return max(0.0, delay) / 1000


def header_override(value):
    """Interpret an X-Payment-Fault header value -> (mode or None, (decision, detail)).

    The header is honoured only when ENABLE_FAULT_INJECTION=1. Otherwise it's ignored (and
    the caller records that it was ignored), so production clients can't choose outcomes.
    """
    if value is None:
        return None, None
    shown = value[:64]
    if not fault_injection_enabled():
        return None, ("ignored", {"value": shown, "reason": "ENABLE_FAULT_INJECTION is not 1"})
    mode = value.strip()
    if mode not in FAULT_MODES:
        return None, ("rejected", {"value": shown, "reason": f"not one of {list(FAULT_MODES)}"})
    return mode, ("applied", {"value": mode})


# ---- the seeded draw ---------------------------------------------------------------------

def draw(seed, key, attempt, rate):
    """The seeded outcome for one attempt: a pure function of its inputs -> (outcome, u).

    u is uniform in [0, 1) from sha256(seed, key, attempt). u < rate means a fault, chosen
    uniformly from FAULTS by the next bytes of the same hash; otherwise success.
    """
    digest = hashlib.sha256(f"{seed}\0{key}\0{attempt}".encode("utf-8")).digest()
    u = int.from_bytes(digest[:8], "big") / 2**64
    if u < rate:
        return FAULTS[int.from_bytes(digest[8:16], "big") % len(FAULTS)], u
    return SUCCESS, u


def _choose(mode, source, key, attempt):
    """-> (outcome, fault) for a mode from `source` ('env' or 'header')."""
    if mode == "random":
        seed, rate = _seed(), _fault_rate()
        outcome, u = draw(seed, key, attempt, rate)
        return outcome, {"source": f"{source}:random", "seed": seed, "idempotency_key": key,
                         "attempt": attempt, "rate": rate, "draw": round(u, 6)}
    outcome = SUCCESS if mode in ("none", SUCCESS) else mode
    return outcome, {"source": source, "mode": mode}


# ---- the ledger --------------------------------------------------------------------------

def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scan(path):
    """Read the ledger -> (entries, problem). problem is None, or a dict describing the first
    bad line: {"line": n, "reason": ..., "last_line": bool, "missing_newline": bool}."""
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as ledger:
            lines = ledger.readlines()
    except FileNotFoundError:
        return entries, None
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        reason = None
        try:
            entry = json.loads(line)
        except ValueError:
            reason = "not valid JSON"
        else:
            if not isinstance(entry, dict) or not isinstance(entry.get("idempotency_key"), str):
                reason = "not a ledger entry"
        if reason:
            return entries, {"line": n, "reason": reason, "last_line": n == len(lines),
                             "missing_newline": not line.endswith("\n")}
        entries.append(entry)
    return entries, None


def _read_entries(path):
    """Every ledger entry -> (entries, problem). Any unreadable line is a problem: the gateway
    then can't tell whether a key was charged, so it refuses to charge (fails closed)."""
    entries, problem = _scan(path)
    return entries, (f"line {problem['line']} is {problem['reason']}" if problem else None)


def verify(path=None):
    """Check the whole ledger -> {"path", "entries", "ok", "problem"}. See AGENTS.md's runbook."""
    path = path or ledger_path()
    entries, problem = _scan(path)
    if problem:
        problem = dict(problem, interrupted_append=problem["last_line"] and problem["missing_newline"])
    return {"path": path, "exists": os.path.exists(path), "entries": len(entries), "ok": problem is None,
            "problem": problem}


def _ride(scooter_id, reservation_id, end_lat, end_lng, amount):
    """A ride's identity: which scooter, which reservation, where it ended, and the amount.
    The end location is compared exactly (floats as the app parsed them): the amount is only
    a rounded distance, so it can't tell two end points at the same distance apart."""
    return (scooter_id, reservation_id, end_lat, end_lng, amount)


def _matches(entry, scooter_id, reservation_id, end_lat, end_lng, amount):
    # An entry written without end coordinates (none should exist) never matches: conflict.
    return _ride(entry.get("scooter_id"), entry.get("reservation_id"), entry.get("end_lat"),
                 entry.get("end_lng"), entry.get("amount")) == _ride(scooter_id, reservation_id, end_lat, end_lng, amount)


@dataclass(frozen=True)
class ChargeResult:
    outcome: str            # one of OUTCOMES or LEDGER_OUTCOMES
    attempt: int            # 1 + the ledger's earlier entries for this key
    txn_id: str | None      # what the caller learns: set for success and already_charged only
    fault: dict             # where the outcome came from (and the draw's inputs)
    charged: bool = False   # ground truth: did THIS call take money? (timeout_charged: yes)
    ledger_txn_id: str | None = None   # ground truth: the txn this call created or found
    detail: dict = field(default_factory=dict)

    @property
    def ok(self):
        """The ride is paid: a new charge the caller knows about, or a matching earlier one."""
        return self.outcome in (SUCCESS, "already_charged")

    def record_detail(self):
        """What a request record should show about this call."""
        return {"gateway_outcome": self.outcome, "txn_id": self.txn_id, "attempt": self.attempt,
                "fault": self.fault, "simulator": {"charged": self.charged, "ledger_txn_id": self.ledger_txn_id},
                **self.detail}


def charge(idempotency_key, scooter_id, reservation_id, amount, *, end_lat, end_lng, request_id=None, override=None):
    """Charge `amount` for one ride, at most once per idempotency key. -> ChargeResult.

    The ride is (scooter_id, reservation_id, end_lat, end_lng, amount); see _ride.
    `override` is an already-gated header mode (see header_override). Raises
    PaymentConfigError for an invalid PAYMENT_* setting (before anything is appended).

    Cost: every call re-reads the whole ledger under its lock (and, from the app, inside the
    DB lock), so a payment is O(ledger size). Fine for a simulator; PLAN PR 15's reconcile
    loop calls this once per stuck ride, so it may want an index by key if the ledger grows.
    """
    path = ledger_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)       # check-then-append is atomic across workers
        entries, problem = _read_entries(path)
        mine = [e for e in entries if e["idempotency_key"] == idempotency_key]
        attempt = len(mine) + 1
        detail = {}
        if problem:
            # Fail closed, and don't append to a ledger we can't read.
            return ChargeResult("error", attempt, None, {"source": "ledger", "reason": "ledger_unreadable",
                                                         "problem": problem},
                                detail={"idempotency_key": idempotency_key, "reservation_id": reservation_id})
        earlier = next((e for e in mine if e.get("charged") is True), None)
        declined = next((e for e in mine if e.get("outcome") == "decline"), None)
        if earlier is not None:
            existing = {"txn_id": earlier.get("txn_id"), "attempt": earlier.get("attempt"),
                        "scooter_id": earlier.get("scooter_id"), "reservation_id": earlier.get("reservation_id"),
                        "end_lat": earlier.get("end_lat"), "end_lng": earlier.get("end_lng"),
                        "amount": earlier.get("amount"), "request_id": earlier.get("request_id")}
            same_ride = _matches(earlier, scooter_id, reservation_id, end_lat, end_lng, amount)
            outcome = "already_charged" if same_ride else "conflict"
            fault = {"source": "ledger"}
            detail = {"existing_charge": existing}
        elif declined is not None and REPLAY_DECLINES:
            # A decline is definitive (H3): the key was declined, so a replay of the same ride
            # (only possible after a DB restore) is declined again, never charged. Charging it
            # would leave a ride both charged and owing (PLAN PR 15b). Another ride under the key
            # is a conflict, as with charges.
            same_ride = _matches(declined, scooter_id, reservation_id, end_lat, end_lng, amount)
            outcome = "decline" if same_ride else "conflict"
            fault = {"source": "ledger", "replayed": "decline"}
            detail = {"existing_decline": {k: declined.get(k) for k in ("attempt", "request_id", "end_lat", "end_lng", "amount")}}
        elif override is not None:
            outcome, fault = _choose(override, "header", idempotency_key, attempt)
        else:
            outcome, fault = _choose(_fault_mode(), "env", idempotency_key, attempt)
        charged = outcome in CHARGING_OUTCOMES
        ledger_txn_id = uuid.uuid4().hex if charged else (earlier.get("txn_id") if earlier is not None else None)
        entry = {"ts": _now_iso(), "idempotency_key": idempotency_key, "attempt": attempt, "outcome": outcome,
                 "charged": charged, "txn_id": ledger_txn_id if charged else None, "scooter_id": scooter_id,
                 "reservation_id": reservation_id, "end_lat": end_lat, "end_lng": end_lng, "amount": amount,
                 "request_id": request_id,
                 "fault": fault, "existing_charge": detail.get("existing_charge"), "pid": os.getpid()}
        delay = _timeout_delay_s() if outcome in TIMEOUTS else 0.0
        line = (json.dumps(entry, ensure_ascii=True, default=str) + "\n").encode("utf-8")
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)                         # closing releases the lock
    if delay:
        time.sleep(delay)                    # a short, simulated wait; never the real thing
    known_txn = ledger_txn_id if outcome in (SUCCESS, "already_charged") else None
    # what this call was actually charged under, so a record can be checked against its reservation
    detail = {"idempotency_key": idempotency_key, "reservation_id": reservation_id, **detail}
    return ChargeResult(outcome, attempt, known_txn, fault, charged, ledger_txn_id, detail)


def all_entries(path=None):
    """Every ledger entry (raises ValueError if the ledger is unreadable)."""
    entries, problem = _read_entries(path or ledger_path())
    if problem:
        raise ValueError(f"ledger unreadable: {problem}")
    return entries


def lookup(idempotency_key, path=None):
    """Every ledger entry for a key, in ledger order."""
    entries, problem = _read_entries(path or ledger_path())
    if problem:
        raise ValueError(f"ledger unreadable: {problem}")
    return [e for e in entries if e["idempotency_key"] == idempotency_key]


# ---- CLI ---------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m payments", description="Inspect the simulated payment gateway.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("draw", help="replay a seeded draw: the outcome for (seed, key, attempt)")
    p.add_argument("--seed", required=True)
    p.add_argument("--key", required=True, help="the reservation's idempotency_key")
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--rate", type=float, default=DEFAULT_FAULT_RATE)
    p = sub.add_parser("lookup", help="every ledger entry for an idempotency key")
    p.add_argument("key")
    p.add_argument("--file", help=f"ledger path (default: $SCOOTER_PAYMENTS_LEDGER_PATH or {DEFAULT_LEDGER_PATH})")
    p = sub.add_parser("verify", help="check every ledger line; report the first bad one (exit 1)")
    p.add_argument("--file", help=f"ledger path (default: $SCOOTER_PAYMENTS_LEDGER_PATH or {DEFAULT_LEDGER_PATH})")
    args = parser.parse_args(argv)
    if args.command == "verify":
        try:
            report = verify(args.file)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report))
        if not report["ok"]:
            p = report["problem"]
            kind = ("an interrupted append (the last line, with no newline)" if p["interrupted_append"]
                    else "NOT just an interrupted append: do not repair by hand, escalate")
            print(f"ledger unreadable at line {p['line']} ({p['reason']}): {kind}. "
                  "See AGENTS.md 'Payments & fault injection' -> 'Unreadable ledger'.", file=sys.stderr)
            return 1
        return 0
    if args.command == "draw":
        outcome, u = draw(args.seed, args.key, args.attempt, args.rate)
        print(json.dumps({"outcome": outcome, "draw": round(u, 6), "seed": args.seed, "idempotency_key": args.key,
                          "attempt": args.attempt, "rate": args.rate}))
        return 0
    try:
        found = lookup(args.key, args.file)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for entry in found:
        print(json.dumps(entry))
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
