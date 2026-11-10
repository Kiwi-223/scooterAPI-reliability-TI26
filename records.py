"""Persisted request records: one JSON line per request (ACTIVITY Outcome 1).

Every request gets a request id (returned as `X-Request-ID`) and, when it finishes,
exactly one JSON line appended to the records file:

    SCOOTER_RECORDS_PATH  (default: data/records.jsonl, relative to the working directory)

Route code adds what it decided with `note(step, outcome, **detail)` and the scooter it
touched with `scooter(...)`; `init_app(app)` wires the per-request bookkeeping into Flask.
The schema is documented in AGENTS.md ("Request records"); `tests/test_records.py` pins it.

Writing is atomic per line: the whole line goes to one O_APPEND file descriptor while an
exclusive `fcntl.flock` is held, so lines from several threads, gunicorn workers or
processes never interleave. If a record can't be written the API response is still
served; the failure is logged at ERROR and the response carries `X-Record-Error`.
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from flask import g, has_request_context, request

DEFAULT_RECORDS_PATH = os.path.join("data", "records.jsonl")
REQUEST_ID_HEADER = "X-Request-ID"
RECORD_ERROR_HEADER = "X-Record-Error"
# A client may send its own X-Request-ID (so it can find its request later). It is only
# honoured if it is 1-64 characters of [A-Za-z0-9._-]; anything else is replaced by a fresh
# id and the rejection is noted, so nothing a client sends can break or forge a log line.
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
MAX_VALUE_CHARS = 1024   # longer param values are truncated in the record (not in the request)
SCHEMA_VERSION = 1

_REPO = Path(__file__).resolve().parent


def records_path():
    return os.environ.get("SCOOTER_RECORDS_PATH") or DEFAULT_RECORDS_PATH


def _git(*args):
    try:
        out = subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _git_state():
    """(commit, dirty) of the code that is running, computed once at startup.

    commit is the full HEAD sha or None; dirty is True if tracked or untracked files differ
    from HEAD (a planted, uncommitted bug shows up here), None if git isn't available.
    """
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return (head.strip() if head else None, None if status is None else bool(status.strip()))


GIT_COMMIT, GIT_DIRTY = _git_state()


def _now_iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip(value):
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + f"...(truncated, {len(value)} chars)"
    return value


def _params():
    params = {}
    for key, values in request.args.lists():
        values = [_clip(v) for v in values]
        params[_clip(key)] = values[0] if len(values) == 1 else values
    return params


def _current():
    return g.get("_record") if has_request_context() else None


# ---- API used by route code -----------------------------------------------------------

def note(step, outcome, **detail):
    """Append a decision: what the request did at `step` and how it came out."""
    record = _current()
    if record is not None:
        record["decisions"].append({"step": step, "outcome": outcome, "detail": detail})


def scooter(scooter_id=None, before=None, after=None):
    """Record the scooter this request is about, and its state before/after (dicts)."""
    record = _current()
    if record is None:
        return
    if scooter_id is not None:
        record["scooter_id"] = _clip(scooter_id)
    if before is not None:
        record["scooter_before"] = dict(before)
    if after is not None:
        record["scooter_after"] = dict(after)


def db_write(ok, error=None):
    record = _current()
    if record is not None:
        record["db_write"] = {"attempted": True, "ok": ok, "error": error}


def failure(kind, msg):
    """Called by the app's error_response(): the failure kind and msg sent to the client."""
    record = _current()
    if record is not None:
        record["error"] = {"kind": kind, "msg": _clip(msg)}
        note("respond", "error", kind=kind)


def exception(exc):
    """Record an exception (type and message) for internal_error / storage_error."""
    record = _current()
    if record is not None and exc is not None:
        record["exception"] = {"type": type(exc).__name__, "message": _clip(str(exc))}


# ---- writing --------------------------------------------------------------------------

def append_line(path, line):
    """Append one line atomically: a single O_APPEND descriptor under an exclusive flock."""
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(data)
            while view:                      # a regular-file write is normally complete in
                written = os.write(fd, view)  # one call; loop (still under the lock) if not
                view = view[written:]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _finish(app, status, response=None):
    record = g.pop("_record", None)
    if record is None:                       # already written (after_request ran)
        return
    started = record.pop("_t0")
    record["status"] = status
    record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    path = records_path()
    try:
        append_line(path, json.dumps(record, ensure_ascii=True, default=str))
    except Exception as exc:                 # never let record keeping break the response
        app.logger.error("REQUEST RECORD NOT WRITTEN: %s (records=%s, request_id=%s)",
                         exc, path, record["request_id"], exc_info=exc)
        if response is not None:
            response.headers[RECORD_ERROR_HEADER] = type(exc).__name__


def init_app(app):
    @app.before_request
    def _begin():
        supplied = request.headers.get(REQUEST_ID_HEADER)
        valid = supplied is not None and _CLIENT_ID_RE.fullmatch(supplied) is not None
        record = {
            "schema": SCHEMA_VERSION,
            "ts": _now_iso(time.time()),
            "request_id": supplied if valid else uuid.uuid4().hex,
            "method": request.method,
            "path": _clip(request.path),
            "params": _params(),
            "scooter_id": None,
            "decisions": [],
            "scooter_before": None,
            "scooter_after": None,
            "db_write": {"attempted": False, "ok": None, "error": None},
            "status": None,
            "error": None,
            "duration_ms": None,
            "git_commit": GIT_COMMIT,
            "git_dirty": GIT_DIRTY,
            "pid": os.getpid(),
            "_t0": time.perf_counter(),
        }
        g._record = record
        if supplied is not None and not valid:
            note("request_id", "client_value_rejected", reason="must be 1-64 chars of [A-Za-z0-9._-]")

    @app.after_request
    def _end(response):
        record = _current()
        if record is not None:
            response.headers[REQUEST_ID_HEADER] = record["request_id"]
            _finish(app, response.status_code, response)
        return response

    @app.teardown_request
    def _teardown(exc):
        # Safety net: runs even if no response was produced (after_request skipped).
        if _current() is not None:
            exception(exc)
            if (_current() or {}).get("error") is None:
                failure("internal_error", "no response was produced")
            _finish(app, 500)
