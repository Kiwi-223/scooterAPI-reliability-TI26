"""MCP server: agent tools over the records, the DB, the ledger and the tests (PLAN PR 16).

ACTIVITY Outcome 4: an agent asks "what failed recently?", "what happened to scooter 18?",
"run the tests" through tools instead of grepping raw files. Registered in `.mcp.json`
(server name `scooter`) and run by Claude Code over stdio:

    .venv/bin/python mcp_server.py          # what .mcp.json runs (stdin/stdout = the protocol)

It's a thin layer: every tool calls the same pure functions as the CLIs (`records_query`,
`reconcile`, `payments`, `tests/invariants.py`); nothing here re-implements a query.

- Data paths come from the same env vars and defaults as the app and the CLIs
  (SCOOTER_DB_PATH, SCOOTER_RECORDS_PATH, SCOOTER_PAYMENTS_LEDGER_PATH); relative paths are
  resolved against the repo root, where the server runs. Every result says which files it
  read (`files_read`).
- Read-only tools never write anything. Three tools act: `run_tests` (runs pytest, whose
  fixtures never touch the live files), `replay_fault` (an isolated temp instance only) and
  `reconcile_run` (changes the live DB, like `make reconcile`); `.claude/settings.json`
  makes all three ask first. MCP servers run outside the Bash sandbox.
- An expected failure (bad argument, unreadable file) is a tool error (`is_error`), with a
  message saying what to fix; it never kills the server.
"""
import datetime
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import app as app_module
import payments
import reconcile
import records_query as rq
from failures import StorageError

REPO = Path(__file__).resolve().parent
NAME = "scooter"
READ_ONLY_TOOLS = ("recent_failures", "failure_counts", "scooter_history", "request_trace", "scooter_state",
                   "anomalies", "ledger_check", "rollback_check", "check_invariants", "payment_lookup",
                   "payment_draw", "reconcile_plan")
ACTION_TOOLS = ("run_tests", "replay_fault", "reconcile_run")     # ask first (.claude/settings.json)

server = MCPServer(
    NAME,
    instructions=("Tools over the scooter API's request records, DB, payment ledger and tests. Start with "
                  "recent_failures / failure_counts, then scooter_history or request_trace for one scooter or "
                  "request. anomalies, ledger_check, rollback_check and check_invariants cross-check the files. "
                  "Read-only tools change nothing; run_tests, replay_fault and reconcile_run ask first. "
                  "The CLI fallback is `make records ARGS=...` (AGENTS.md, 'Agent tools (MCP)')."),
)


# ---- shared plumbing ----------------------------------------------------------------------

def _path(env, default):
    return Path(os.environ.get(env) or default)


SCENARIO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def data_paths(scenario=None):
    """The live data files, exactly as the app and the CLIs resolve them; or, with
    `scenario`, the isolated files of the latest `make scenario NAME=<scenario>` run
    (data/scenarios/<scenario>/, PLAN PR 17)."""
    if scenario:
        base = Path(os.environ.get("SCOOTER_SCENARIOS_DIR") or REPO / "data" / "scenarios") / scenario
        if not SCENARIO_RE.match(scenario) or not base.is_dir():
            raise ToolError(f"no scenario run {scenario!r} under data/scenarios/; run `make scenario NAME={scenario}`"
                            if SCENARIO_RE.match(scenario) else "scenario: lowercase letters, digits and - only")
        return {"db": base / "db.json", "records": base / "records.jsonl", "ledger": base / "ledger.jsonl"}
    return {"db": _path("SCOOTER_DB_PATH", app_module.DEFAULT_DB_PATH),
            "records": Path(rq.records_path()),
            "ledger": Path(payments.ledger_path())}


def _file(path):
    path = Path(path)
    return {"path": str(path.resolve()), "exists": path.exists()}


def _records(scenario=None):
    """All records (a missing file means none yet) -> (records, files_read entry)."""
    path = data_paths(scenario)["records"]
    warnings = []
    try:
        records = list(rq.read_records(path, warn=lambda n, reason: warnings.append(f"line {n}: {reason}")))
    except FileNotFoundError:
        records = []
    except OSError as exc:
        raise ToolError(f"cannot read the records file {path}: {exc.strerror or exc}") from None
    return records, dict(_file(path), records=len(records), skipped_lines=warnings[:20])


def _db_raw(scenario=None):
    path = data_paths(scenario)["db"]
    db, problem = rq.read_db(str(path))
    if problem:
        raise ToolError(f"cannot read the DB: {problem}")
    return db, _file(path)


def _ledger(scenario=None):
    path = data_paths(scenario)["ledger"]
    try:
        entries = payments.all_entries(str(path))
    except ValueError as exc:
        raise ToolError(f"{exc} ({path}); see AGENTS.md 'Unreadable ledger', or run `make payments ARGS=verify`") from None
    return entries, dict(_file(path), entries=len(entries))


def _since(since):
    if since is None or since == "":
        return None
    try:
        return rq.parse_since(since)
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def _result(result, **files):
    return {"result": result, "files_read": files}


# ---- read-only tools --------------------------------------------------------------------------

@server.tool()
def recent_failures(since: str | None = None, limit: int = 20, scenario: str | None = None) -> dict[str, Any]:
    """What failed recently: the newest failed requests (error != null), newest first.
    since: a duration (30s, 15m, 1h, 2d) or an ISO-8601 time. Same as `make records ARGS="recent-failures"`.
    scenario (every read-only tool): read a `make scenario NAME=<scenario>` run instead of the live files."""
    if not 1 <= limit <= 500:
        raise ToolError("limit must be between 1 and 500")
    records, read = _records(scenario)
    return _result(rq.recent_failures(records, _since(since), limit), records=read)


@server.tool()
def failure_counts(since: str | None = None, scenario: str | None = None) -> dict[str, Any]:
    """How many failures of each error.kind, most frequent first (`make records ARGS="failure-counts"`)."""
    records, read = _records(scenario)
    return _result(rq.failure_counts(records, _since(since)), records=read)


@server.tool()
def scooter_history(scooter_id: str, scenario: str | None = None) -> dict[str, Any]:
    """What happened to one scooter: every record about it, in the order the requests took
    effect (lock order), with before/after state and decisions (`make records ARGS="scooter <id>"`)."""
    records, read = _records(scenario)
    return _result(rq.scooter_history(records, scooter_id), records=read)


@server.tool()
def request_trace(request_id: str, scenario: str | None = None) -> dict[str, Any]:
    """The full record(s) for one X-Request-ID: every decision the request made (`make records ARGS="request <id>"`)."""
    records, read = _records(scenario)
    found = rq.find_requests(records, request_id)
    if not found:
        raise ToolError(f"no record with request_id {request_id!r} in {read['path']}")
    return _result(found, records=read)


@server.tool()
def scooter_state(scooter_id: str = "all", scenario: str | None = None) -> dict[str, Any]:
    """The DB's current state of one scooter (or "all"), as the app loads it: reservation,
    payment state, outstanding balances. Validated like every app read; read-only, no lock."""
    path = data_paths(scenario)["db"]
    try:
        db = app_module.init_db(str(path))
    except StorageError as exc:
        raise ToolError(f"the DB is unusable: {exc}") from None
    scooters = [s.to_dict() for s in db]
    if scooter_id != "all":
        scooters = [s for s in scooters if s["id"] == scooter_id]
        if not scooters:
            raise ToolError(f"no scooter with id {scooter_id!r} in {path}")
    return _result(scooters, db=_file(path))


@server.tool()
def anomalies(since: str | None = None, scenario: str | None = None) -> dict[str, Any]:
    """Records whose fields contradict each other (the 19 rules in AGENTS.md). `unresolved`
    is what needs attention; a resolved finding is history (`make records ARGS="anomalies"`)."""
    records, read = _records(scenario)
    path = data_paths(scenario)["db"]
    db, problem = rq.read_db(str(path))
    found = rq.anomalies(records, _since(since), db, db_path=str(path))
    return _result({"findings": found, "unresolved": len(rq.unresolved(found)), "db_problem": problem},
                   records=read, db=_file(path))


@server.tool()
def ledger_check(min_age_s: float = 5.0, scenario: str | None = None) -> dict[str, Any]:
    """The payments ledger against the records: double charges, charges under foreign keys,
    requests that died mid-payment (orphans). `problems` is true if any needs attention."""
    records, read = _records(scenario)
    path = data_paths(scenario)["ledger"]
    try:
        entries = rq.read_ledger(str(path))
    except FileNotFoundError:
        entries = []
    except OSError as exc:
        raise ToolError(f"cannot read the ledger {path}: {exc.strerror or exc}") from None
    result = rq.ledger_check(entries, records, min_age_s=min_age_s)
    return _result(dict(result, problems=rq.ledger_problems(result)), records=read,
                   ledger=dict(_file(path), entries=len(entries)))


@server.tool()
def rollback_check(since: str | None = None, scenario: str | None = None) -> dict[str, Any]:
    """What a DB rollback (git restore scooter_db.json, a backup) erased: lost balances
    (reconcile rebuilds them), rides erased in progress (free rides) and records-only balances."""
    records, read = _records(scenario)
    db, db_read = _db_raw(scenario)
    entries, ledger_read = _ledger(scenario)
    result = rq.rollback_check(records, db, entries, _since(since))
    return _result(dict(result, problems=rq.rollback_problems(result)), records=read, db=db_read, ledger=ledger_read)


def _invariants_module():
    spec = importlib.util.spec_from_file_location("scooter_invariants", REPO / "tests" / "invariants.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@server.tool()
def check_invariants(scenario: str | None = None) -> dict[str, Any]:
    """The end-ride invariants I1-I5 across the DB, the ledger and the records (as
    `.venv/bin/python tests/invariants.py`). An empty `violations` list means all hold."""
    records, read = _records(scenario)
    db, db_read = _db_raw(scenario)
    entries, ledger_read = _ledger(scenario)
    inv = _invariants_module()
    found = inv.violations(db, entries, inv.started_keys(records, db), inv.pending_amounts(records))
    return _result({"violations": [list(v) for v in found], "ok": not found},
                   records=read, db=db_read, ledger=ledger_read)


@server.tool()
def payment_lookup(idempotency_key: str, scenario: str | None = None) -> dict[str, Any]:
    """Every ledger entry for one idempotency key (= reservation_id), in order: each
    attempt's outcome, whether it charged, and why (`make payments ARGS="lookup <key>"`)."""
    entries, read = _ledger(scenario)
    return _result([e for e in entries if e.get("idempotency_key") == idempotency_key], ledger=read)


@server.tool()
def payment_draw(seed: str, idempotency_key: str, attempt: int, rate: float = payments.DEFAULT_FAULT_RATE) -> dict[str, Any]:
    """Recompute a seeded random fault: the outcome PAYMENT_FAULT=random gives for (seed,
    key, attempt, rate). Copy them from a record's payment `fault`. Pure: reads no file."""
    if attempt < 1 or not 0 <= rate <= 1:
        raise ToolError("attempt must be >= 1 and rate in [0, 1]")
    outcome, u = payments.draw(seed, idempotency_key, attempt, rate)
    return _result({"outcome": outcome, "draw": round(u, 6), "seed": seed, "idempotency_key": idempotency_key,
                    "attempt": attempt, "rate": rate})


def _reconcile_limits(max_attempts, max_age_minutes):
    if max_attempts is not None and max_attempts < 1 or max_age_minutes is not None and max_age_minutes < 0:
        raise ToolError("max_attempts must be >= 1 and max_age_minutes >= 0")
    attempts = max_attempts if max_attempts is not None else \
        reconcile._env_number("RECONCILE_MAX_ATTEMPTS", reconcile.DEFAULT_MAX_ATTEMPTS, int)
    minutes = max_age_minutes if max_age_minutes is not None else \
        reconcile._env_number("RECONCILE_MAX_AGE_MINUTES", reconcile.DEFAULT_MAX_AGE_MINUTES, float)
    return attempts, datetime.timedelta(minutes=minutes)


def _reconcile(dry_run, max_attempts, max_age_minutes, only):
    attempts, age = _reconcile_limits(max_attempts, max_age_minutes)
    paths = data_paths()
    try:
        summary = reconcile.run(max_attempts=attempts, max_age=age, dry_run=dry_run, only=only)
    except StorageError as exc:
        raise ToolError(f"the DB is unusable: {exc}") from None
    return _result(dict(summary, remaining=reconcile.remaining(summary)),
                   db=_file(paths["db"]), ledger=_file(paths["ledger"]))


@server.tool()
def reconcile_plan(max_attempts: int | None = None, max_age_minutes: float | None = None,
                   only: str | None = None) -> dict[str, Any]:
    """What `make reconcile` would do now (a dry run: changes nothing, writes no records):
    one decision per scooter that needs one, with the action and why."""
    return _reconcile(True, max_attempts, max_age_minutes, only)


# ---- action tools (ask first) ----------------------------------------------------------------

@server.tool()
def reconcile_run(max_attempts: int | None = None, max_age_minutes: float | None = None,
                  only: str | None = None) -> dict[str, Any]:
    """CHANGES THE LIVE DB: `make reconcile`. Settles stuck rides, voids paid balances,
    rebuilds lost ones; each action is a locked transaction with its own request record."""
    return _reconcile(False, max_attempts, max_age_minutes, only)


TEST_PATH_RE = re.compile(r"^tests/test_[A-Za-z0-9_]+\.py(::[A-Za-z0-9_]+(\[[A-Za-z0-9_.\-]*\])?)?$")
KEYWORD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ()\-.\[\]]*$")
TAIL_MAX, TIMEOUT_MAX = 400, 900
NESTED = "SCOOTER_MCP_RUN_TESTS"


# Variables that change what pytest or Python runs, so a validated argv can't be widened
# from the environment: PYTEST_ADDOPTS adds options, PYTEST_PLUGINS loads plugins, and
# PYTHONPATH / PYTHONSTARTUP / PYTHONHOME / PYTHONINSPECT / PYTHONBREAKPOINT etc. load or run
# other code. Every PYTEST_* and PYTHON* variable is dropped (PR #23 review).
SCRUBBED_ENV_PREFIXES = ("PYTEST_", "PYTHON", "_PYTEST")


def child_env(environ=None):
    """The environment run_tests gives pytest: the server's, minus everything that could
    change what runs, plus the nesting marker."""
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if not k.upper().startswith(SCRUBBED_ENV_PREFIXES)}
    env.update({NESTED: "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def pytest_argv(paths=None, keyword=None, exitfirst=False):
    """The validated pytest command: only test files under tests/, an optional -k expression
    of plain words, and -x. No other option, no shell."""
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    for p in paths or []:
        if not TEST_PATH_RE.match(p) or not (REPO / p.split("::")[0]).is_file():
            raise ToolError(f"paths: {p!r} isn't a test file (or file::test) under tests/")
    argv += list(paths or [])
    if keyword is not None:
        if not KEYWORD_RE.match(keyword) or len(keyword) > 200:
            raise ToolError("keyword: letters, digits, _ - . [ ] ( ) and spaces only (a pytest -k expression)")
        argv += ["-k", keyword]
    if exitfirst:
        argv.append("-x")
    return argv


@server.tool()
def run_tests(paths: list[str] | None = None, keyword: str | None = None, exitfirst: bool = False,
              timeout_s: int = 600, tail_lines: int = 60) -> dict[str, Any]:
    """Run the test suite (as `make test`), or a validated subset: test files under tests/
    (`tests/test_x.py` or `tests/test_x.py::test_name`), a -k keyword, -x. Returns the exit
    code, the summary line and the last tail_lines lines. The tests never touch the live files.
    PYTEST_* and PYTHON* variables are dropped from the child's environment. NOTE: this runs
    the working tree's test code (and conftest.py) outside the Bash sandbox; prefer `make
    test` (sandboxed) and approve this only for tests you've reviewed."""
    if os.environ.get(NESTED):
        raise ToolError("run_tests is already running the tests (no nested runs)")
    if not 1 <= timeout_s <= TIMEOUT_MAX or not 1 <= tail_lines <= TAIL_MAX:
        raise ToolError(f"timeout_s must be 1..{TIMEOUT_MAX} and tail_lines 1..{TAIL_MAX}")
    argv = pytest_argv(paths, keyword, exitfirst)
    proc = subprocess.Popen(argv, cwd=REPO, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)          # the whole pytest process group
        out, _ = proc.communicate()
        timed_out = True
    lines = out.splitlines()
    summary = next((line for line in reversed(lines) if line.strip()), "")
    return {"command": " ".join(argv[1:]), "exit_code": proc.returncode, "passed": proc.returncode == 0 and not timed_out,
            "timed_out": timed_out, "summary": summary, "tail": [line[:500] for line in lines[-tail_lines:]]}


_REPLAY = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import app
args = json.loads(sys.argv[2])
headers = {"X-Request-ID": args["request_id"]}
if args.get("header"):
    headers["X-Payment-Fault"] = args["header"]
resp = app.app.test_client().get("/reservation/end", query_string=args["params"], headers=headers)
print(json.dumps({"status": resp.status_code, "body": resp.get_json()}))
"""
FAULT_ENV = ("PAYMENT_FAULT", "PAYMENT_SEED", "PAYMENT_FAULT_RATE", "ENABLE_FAULT_INJECTION")


def _gateway_call(record):
    """The record's last gateway call: its payment ok/failed decision's detail."""
    calls = [d for d in record.get("decisions") or [] if isinstance(d, dict) and d.get("step") == "payment"
             and d.get("outcome") in ("ok", "failed") and isinstance(d.get("detail"), dict)]
    return calls[-1]["detail"] if calls else None


def _summary(record, call):
    return {"request_id": record.get("request_id"), "status": record.get("status"),
            "error_kind": (record.get("error") or {}).get("kind"),
            "gateway_outcome": (call or {}).get("gateway_outcome"), "fault": (call or {}).get("fault")}


def replay_env(fault, override=None):
    """The fault env (and header) that reproduce a recorded `fault` provenance."""
    env, header = {"PAYMENT_TIMEOUT_DELAY_MS": "0"}, None
    if override is not None:
        env["PAYMENT_FAULT"] = override
        return env, header
    source = (fault or {}).get("source", "")
    if source.endswith(":random"):
        env.update(PAYMENT_SEED=str(fault["seed"]), PAYMENT_FAULT_RATE=str(fault["rate"]))
        mode = "random"
    else:
        mode = fault.get("mode")             # None for "ledger": the copied ledger decides
    if source.startswith("header"):
        env["ENABLE_FAULT_INJECTION"] = "1"
        header = mode
    elif mode is not None:
        env["PAYMENT_FAULT"] = mode
    return env, header


@server.tool()
def replay_fault(request_id: str, fault: str | None = None, scenario: str | None = None) -> dict[str, Any]:
    """Reproduce a recorded /reservation/end payment failure on an ISOLATED temp instance
    (temp DB, records and ledger; the live files are only read). It rebuilds the scooter as
    the record saw it and the key's earlier ledger entries, re-applies the record's fault
    provenance (env, header or seeded draw), and returns the replayed record. fault:
    optionally force another outcome instead ("what if it had been a decline?"). scenario:
    replay a request from a `make scenario` run."""
    if fault is not None and fault not in payments.FAULT_MODES:
        raise ToolError(f"fault must be one of {list(payments.FAULT_MODES)}")
    records, read = _records(scenario)
    original = next((r for r in rq.find_requests(records, request_id) if r.get("path") == "/reservation/end"), None)
    if original is None:
        raise ToolError(f"no /reservation/end record with request_id {request_id!r} in {read['path']}")
    call = _gateway_call(original)
    before = original.get("scooter_before")
    if call is None or not isinstance(before, dict) or not call.get("idempotency_key"):
        raise ToolError("that request never called the payment gateway, so there's no fault to replay")
    db, db_read = _db_raw(scenario)
    if not any(isinstance(s, dict) and s.get("id") == before.get("id") for s in db):
        raise ToolError(f"scooter {before.get('id')!r} isn't in the current DB")
    ledger, ledger_read = _ledger(scenario)
    key, attempt = call["idempotency_key"], call.get("attempt") or 1
    earlier = [e for e in ledger if e.get("idempotency_key") == key][:attempt - 1]
    env_fault, header = replay_env(call.get("fault"), fault)
    live = {str(p.resolve()) for p in [*data_paths().values(), *data_paths(scenario).values()]}
    with tempfile.TemporaryDirectory(prefix="scooter-replay-") as tmp:
        tmp = Path(tmp).resolve()
        paths = {"SCOOTER_DB_PATH": tmp / "db.json", "SCOOTER_RECORDS_PATH": tmp / "records.jsonl",
                 "SCOOTER_PAYMENTS_LEDGER_PATH": tmp / "ledger.jsonl"}
        # the guard: everything the replay can write is inside the temp dir, never a live file
        if not all(p.parent == tmp and str(p) not in live for p in paths.values()):
            raise ToolError("refusing to replay: the temp instance's paths would include a live data file")
        paths["SCOOTER_DB_PATH"].write_text(json.dumps([before if s.get("id") == before["id"] else s for s in db]))
        paths["SCOOTER_PAYMENTS_LEDGER_PATH"].write_text("".join(json.dumps(e) + "\n" for e in earlier))
        env = {k: v for k, v in os.environ.items() if k not in FAULT_ENV and not k.startswith("SCOOTER_")}
        env.update({k: str(v) for k, v in paths.items()}, **env_fault, PYTHONDONTWRITEBYTECODE="1")
        replay_id = f"replay-{request_id}"[:64] if re.fullmatch(r"[A-Za-z0-9._-]{1,57}", request_id) else "replay"
        args = {"request_id": replay_id, "params": original.get("params") or {}, "header": header}
        try:
            out = subprocess.run([sys.executable, "-c", _REPLAY, str(REPO), json.dumps(args)], cwd=tmp, env=env,
                                 capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise ToolError("the replay didn't finish within 60 s") from None
        if out.returncode != 0:
            raise ToolError(f"the replay failed: {out.stderr.strip()[-2000:]}")
        response = json.loads(out.stdout)
        replayed = list(rq.read_records(paths["SCOOTER_RECORDS_PATH"])) if paths["SCOOTER_RECORDS_PATH"].exists() else []
        new_ledger = payments.all_entries(str(paths["SCOOTER_PAYMENTS_LEDGER_PATH"]))[len(earlier):]
    record = replayed[-1] if replayed else {}
    mine, theirs = _summary(record, _gateway_call(record)), _summary(original, call)
    same = all(mine[k] == theirs[k] for k in ("status", "error_kind", "gateway_outcome"))
    return _result({"reproduced": same if fault is None else None, "original": theirs, "replayed": mine,
                    "response": response, "replayed_records": replayed, "replayed_ledger_entries": new_ledger,
                    "fault_env": env_fault, "fault_header": header, "earlier_ledger_entries": len(earlier),
                    "isolated": "a temp DB/records/ledger, deleted afterwards; live files were only read"},
                   records=read, db=db_read, ledger=ledger_read)


def main():
    os.chdir(REPO)              # relative data paths resolve against the repo root, like the CLIs
    server.run("stdio")


if __name__ == "__main__":
    main()
