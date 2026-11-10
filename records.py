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
MAX_PARAMS = 50          # at most this many distinct param keys are recorded (params_truncated)
MAX_DIRTY_PATHS = 20     # at most this many changed paths are listed in git_dirty_paths
# Files the app itself rewrites. A change to them is data, not code, so it doesn't make the
# code "dirty" (a reservation against the live DB rewrites scooter_db.json).
DATA_PATHS = {"scooter_db.json"}
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


def _changed_paths(porcelain_z):
    """Paths from `git status --porcelain=v1 -z` output (renames give their new path)."""
    entries = porcelain_z.split("\0")
    paths, i = [], 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1                              # the next entry is the original path
        paths.append(path)
    return paths


def _git_state():
    """(commit, dirty, dirty_paths) of the source tree, computed once at startup.

    commit: the full HEAD sha, or None. dirty: True if any *code* file (tracked or untracked,
    not git-ignored) differs from HEAD, i.e. how a planted, uncommitted bug shows up; data
    files in DATA_PATHS don't count. dirty_paths: those code paths (capped). dirty and
    dirty_paths are None if git isn't available.
    """
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status is None:
        return (head.strip() if head else None, None, None)
    code_paths = sorted(p for p in _changed_paths(status) if p not in DATA_PATHS)
    return (head.strip() if head else None, bool(code_paths), code_paths[:MAX_DIRTY_PATHS])


GIT_COMMIT, GIT_DIRTY, GIT_DIRTY_PATHS = _git_state()


def _now_iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip(value):
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + f"...(truncated, {len(value)} chars)"
    return value


def _params():
    """The query args (repeated keys as lists), capped at MAX_PARAMS keys -> (params, truncated)."""
    params = {}
    for n, (key, values) in enumerate(request.args.lists()):
        if n == MAX_PARAMS:
            return params, True
        values = [_clip(v) for v in values]
        params[_clip(key)] = values[0] if len(values) == 1 else values
    return params, False


def _current():
    return g.get("_record") if has_request_context() else None


# ---- API used by route code -----------------------------------------------------------

def current_request_id():
    """The current request's id (its X-Request-ID), or None outside a request."""
    record = _current()
    return record["request_id"] if record is not None else None


def note(step, outcome, **detail):
    """Append a decision: what the request did at `step` and how it came out."""
    record = _current()
    if record is not None:
        record["decisions"].append({"step": step, "outcome": outcome, "detail": detail})


def scooter(scooter_id=None, before=None, in_memory=None):
    """Record the scooter this request is about, its persisted state before the request,
    and the state the route's in-memory object ended with (dicts)."""
    record = _current()
    if record is None:
        return
    if scooter_id is not None:
        record["scooter_id"] = _clip(scooter_id)
    if before is not None:
        record["scooter_before"] = dict(before)
    if in_memory is not None:
        record["scooter_in_memory"] = dict(in_memory)


def unchanged():
    """The request decided not to write: the persisted state is still scooter_before."""
    record = _current()
    if record is not None and record["scooter_before"] is not None and not record["db_write"]["attempted"]:
        record["scooter_after"] = dict(record["scooter_before"])


def written(db_list):
    """Called by write_db with the exact list it serialised: scooter_after becomes this
    request's scooter as written to disk, not as the code believes it to be."""
    record = _current()
    if record is None or record["scooter_id"] is None:
        return
    entries = [s for s in db_list if isinstance(s, dict) and s.get("id") == record["scooter_id"]]
    record["scooter_after"] = dict(entries[0]) if len(entries) == 1 else None


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
            "params": None,
            "params_truncated": False,
            "scooter_id": None,
            "decisions": [],
            "scooter_before": None,
            "scooter_after": None,
            "scooter_in_memory": None,
            "db_write": {"attempted": False, "ok": None, "error": None},
            "status": None,
            "error": None,
            "duration_ms": None,
            "git_commit": GIT_COMMIT,
            "git_dirty": GIT_DIRTY,
            "git_dirty_paths": GIT_DIRTY_PATHS,
            "pid": os.getpid(),
            "_t0": time.perf_counter(),
        }
        record["params"], record["params_truncated"] = _params()
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
