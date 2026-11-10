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
    """Every record about one scooter, oldest first by ts (file order is completion order)."""
    return sorted((r for r in records if r.get("scooter_id") == scooter_id), key=_ts_key)


def find_requests(records, request_id):
    """Records with this request_id (normally one; a reused client-supplied id gives more)."""
    return [r for r in records if r.get("request_id") == request_id]


def _float_pair(lat, lng):
    try:
        return (float(lat), float(lng))
    except (TypeError, ValueError):
        return None


def _requested_end(params):
    """The lat/lng an end request asked for, as the app parsed them (first value of each)."""
    if not isinstance(params, dict):
        return None
    first = lambda v: v[0] if isinstance(v, list) and v else v
    return _float_pair(first(params.get("lat")), first(params.get("lng")))


def anomalies(records, since=None):
    """Records whose own fields contradict each other. Each finding is a dict with
    `type`, `request_id`, `ts`, `path`, `scooter_id`, `status` and `detail`.

    - write_not_persisted: db_write.ok but scooter_after != scooter_in_memory (the code
      changed the scooter, but what was written doesn't contain that change)
    - success_without_write: a /reservation/* request returned 200 without attempting a write
    - end_left_reserved: /reservation/end returned 200 but scooter_after is still reserved
    - start_left_free: /reservation/start returned 200 but scooter_after isn't reserved
    - end_location_not_persisted: /reservation/end returned 200 but scooter_after's lat/lng
      differ from the requested end coordinates (compared as floats)
    - reservation_flag_mismatch: scooter_after breaks the DB invariant "is_reserved is true
      exactly when reservation is set" (PLAN PR 12)
    - start_reservation_not_persisted: /reservation/start returned 200 but scooter_after has
      no reservation created by this request (its start_request_id isn't this request_id)

    The two reservation rules only look at snapshots that have a "reservation" key: records
    written before PLAN PR 12 have none, and aren't flagged for it.

    Deliberately not included: a cross-record continuity check (a scooter's scooter_before
    should equal its previous scooter_after). Concurrent workers, `git restore` of the DB
    and manual edits all break that chain legitimately, so it would mostly be noise; the
    per-record rules above catch persistence bugs inside the request that caused them.
    """
    found = []
    for r in records:
        if not in_window(r, since):
            continue
        db_write = r.get("db_write") or {}
        after, in_memory = r.get("scooter_after"), r.get("scooter_in_memory")
        status, path = r.get("status"), r.get("path")

        def flag(kind, detail):
            found.append({"type": kind, "request_id": r.get("request_id"), "ts": r.get("ts"),
                          "path": path, "scooter_id": r.get("scooter_id"), "status": status,
                          "detail": detail})

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
            wanted = _requested_end(r.get("params"))
            got = _float_pair(after.get("lat"), after.get("lng"))
            if wanted is not None and got != wanted:
                flag("end_location_not_persisted", {"requested": {"lat": wanted[0], "lng": wanted[1]},
                                                    "scooter_after": after})
        if isinstance(after, dict) and "reservation" in after:
            reservation = after["reservation"]
            if (after.get("is_reserved") is True) != (reservation is not None):
                flag("reservation_flag_mismatch", {"scooter_after": after})
            if path == "/reservation/start" and status == 200 and (
                    not isinstance(reservation, dict) or reservation.get("start_request_id") != r.get("request_id")):
                flag("start_reservation_not_persisted", {"scooter_after": after})
    return sorted(found, key=_ts_key)


# ---- CLI ------------------------------------------------------------------------------

def _summary(record):
    error = record.get("error") or {}
    return (f"{record.get('ts', '?'):24} {record.get('status', '?')!s:>3} "
            f"{(error.get('kind') or 'ok'):25} {record.get('method', '?'):4} {record.get('path', '?')} "
            f"id={record.get('request_id')} scooter={record.get('scooter_id')}")


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
            print(f"  {_summary(r)}\n      before={_state(r.get('scooter_before'))} "
                  f"after={_state(r.get('scooter_after'))} in_memory={_state(r.get('scooter_in_memory'))}")
    elif command == "request":
        for record in result:
            print(json.dumps(record, indent=2, ensure_ascii=False))
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
