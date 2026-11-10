"""Reproducible scenarios: realistic traffic against an ISOLATED instance (PLAN PR 17).

    make scenario NAME=rides             # one scenario
    make scenario NAME=all               # every scenario, each in its own directory
    make scenario                        # list them
    .venv/bin/python scenarios.py rides --out /tmp/x     # anywhere else

A fresh checkout has no records, so a debugging agent reproduces the symptom here first.
Each scenario runs the **working-tree code** (a fresh interpreter, with a private bytecode
cache so stale `__pycache__` can't be used) against its own DB, records and ledger in
`data/scenarios/<name>/` (the previous run of that name is moved to
`data/scenarios/<name>.<timestamp>/`, never deleted). The DB starts as the committed
`scooter_db.json` (`git show HEAD:scooter_db.json`). The live files are never touched.

Every step says what it expected (status, failure kind, what the DB or
`/view_all_available` should show afterwards). A planted bug shows up as `UNEXPECTED`
steps plus the anomalies / invariant violations / ledger problems found in the evidence;
the run then exits 1. It prints where the evidence is and the exact commands to query it.

Determinism: the same steps, faults and request ids (`scn-<name>-<n>`) every run; faults are
forced per request (`X-Payment-Fault`, with `ENABLE_FAULT_INJECTION=1` inside the instance
only), never random. Reservation ids (uuids) differ between runs.
"""
import argparse
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEFAULT_ROOT = REPO / "data" / "scenarios"       # or $SCOOTER_SCENARIOS_DIR (the MCP server reads the same)
FILES = {"db": "db.json", "records": "records.jsonl", "ledger": "ledger.jsonl", "steps": "steps.json",
         "summary": "summary.json"}
SEED_DB = REPO / "tests" / "seed_db.json"


# ---- the scenarios (run in the child, against the working-tree app) ---------------------------

def rides(d):
    """Normal rides: view, search, start and end, a legacy reservation."""
    d.expect_available("18", True, "18 starts free")
    d.req("/reservation/start?id=18", 200, "start a ride on 18")
    d.expect_available("18", False, "a reserved scooter isn't listed")
    d.req("/reservation/end?id=18&lat=23.01&lng=19.01", 200, "end the ride on 18 (paid)")
    d.expect_available("18", True, "after its ride ended, 18 is listed again (the ACTIVITY symptom)")
    d.expect_db("18", lambda s: s["is_reserved"] is False and s.get("reservation") is None
                and (s["lat"], s["lng"]) == (23.01, 19.01), "18 is free at (23.01, 19.01) in the DB")
    d.req("/search?lat=23&lng=19&radius=5000", 200, "search near 18",
          check=lambda b: "18" in [s["id"] for s in b], check_why="the search finds 18")
    d.req("/reservation/start?id=15", 200, "start a ride on 15")
    d.req("/reservation/end?id=15&lat=0.01&lng=-129.01", 200, "end the ride on 15")
    d.expect_available("15", True, "15 is listed again")
    d.req("/reservation/start?id=33", (422, "scooter_already_reserved"), "33 is reserved in the committed DB")
    d.req("/reservation/end?id=33&lat=24.5&lng=-28", 200, "end 33's legacy ride")
    d.expect_available("33", True, "33 is listed after its legacy ride ended")


def errors(d):
    """Validation and refusals: every client error has its failure kind, nothing changes."""
    d.req("/search?lat=1&lng=2", (422, "missing_param"), "search without radius")
    d.req("/search?lat=abc&lng=2&radius=5", (422, "invalid_number"), "non-numeric lat")
    d.req("/search?lat=91&lng=2&radius=5", (422, "invalid_coordinate"), "lat out of range")
    d.req("/search?lat=1&lng=181&radius=5", (422, "invalid_coordinate"), "lng out of range")
    d.req("/search?lat=1&lng=2&radius=-1", (422, "invalid_radius"), "negative radius")
    d.req("/search?lat=1&lng=2&radius=x", (422, "invalid_number"), "non-numeric radius")
    d.req("/reservation/start?id=999", (422, "scooter_not_found"), "unknown scooter")
    d.req("/reservation/start", (422, "missing_param"), "start without id")
    d.req("/reservation/end?id=9&lat=0&lng=0", (422, "no_active_reservation"), "end a free scooter")
    d.req("/reservation/start?id=18", 200, "start 18")
    d.req("/reservation/end?id=18&lat=91&lng=19", (422, "invalid_coordinate"), "end with lat out of range")
    d.req("/reservation/end?id=18&lat=23&lng=x", (422, "invalid_number"), "end with non-numeric lng")
    d.expect_db("18", lambda s: s["is_reserved"] and s["reservation"]["payment_state"] == "active",
                "a refused end leaves 18's ride active")
    d.req("/reservation/end?id=18&lat=23&lng=19.5", 200, "a valid end still works")
    d.req("/nope", (404, "route_not_found"), "unknown path")
    d.req("/view_all_available", (405, "method_not_allowed"), "wrong method", method="POST")


FAULT_CASES = [("18", "decline", (422, "payment_declined")), ("15", "timeout_not_charged", (504, "payment_timeout")),
               ("9", "timeout_charged", (504, "payment_timeout")), ("14", "error", (502, "payment_failed"))]


def payment_faults(d):
    """Each payment fault once, then retries settle the pending ones."""
    for sid, fault, expected in FAULT_CASES:
        d.req(f"/reservation/start?id={sid}", 200, f"start {sid}")
        d.req(f"/reservation/end?id={sid}&lat=1&lng=1", expected, f"end {sid} with {fault}", fault=fault)
    d.expect_db("18", lambda s: not s["is_reserved"] and len(open_balances(s)) == 1,
                "a decline ends the ride with an outstanding balance (H3)")
    for sid in ("15", "9", "14"):
        d.expect_db(sid, lambda s: s["is_reserved"] and s["reservation"]["payment_state"] == "payment_pending",
                    f"{sid}'s ride is payment_pending after a timeout or error")
        d.expect_available(sid, False, f"{sid} isn't listed while its payment is pending")
        d.expect_found(sid, False, f"{sid} isn't found by search while its payment is pending")
        d.req(f"/reservation/start?id={sid}", (422, "scooter_payment_pending"), f"{sid} can't be started while pending")
    for sid in ("15", "9", "14"):
        d.req(f"/reservation/end?id={sid}&lat=5&lng=5", 200, f"a retry settles {sid} (at the stored location)")
        d.expect_db(sid, lambda s: not s["is_reserved"] and (s["lat"], s["lng"]) == (1.0, 1.0),
                    f"{sid} is free at the stored end location (1, 1)")
    d.expect_ledger(lambda entries: sum(e["charged"] for e in entries) == 3,
                    "exactly 3 charges: one per paid ride, none for the decline, none twice")


def retry_settle(d):
    """A charged timeout settles with its charge; an error retried elsewhere ends at the stored spot."""
    d.req("/reservation/start?id=18", 200, "start 18")
    d.req("/reservation/end?id=18&lat=23.01&lng=19.01", (504, "payment_timeout"), "charged, but timed out",
          fault="timeout_charged")
    d.req("/reservation/end?id=18&lat=23.01&lng=19.01", 200, "the retry settles with the earlier charge")
    d.expect_available("18", True, "18 is free after the retry")
    d.expect_ledger(lambda entries: [e["outcome"] for e in entries] == ["timeout_charged", "already_charged"],
                    "the retry wasn't charged again")
    d.req("/reservation/start?id=15", 200, "start 15")
    d.req("/reservation/end?id=15&lat=0.01&lng=-129.01", (502, "payment_failed"), "gateway error", fault="error")
    d.req("/reservation/end?id=15&lat=10&lng=10", 200, "retry from elsewhere")
    d.expect_db("15", lambda s: (s["lat"], s["lng"]) == (0.01, -129.01), "15 ends where it was first asked to")


def decline(d):
    """A decline ends the ride with a balance; the scooter can be ridden again."""
    d.req("/reservation/start?id=18", 200, "start 18")
    d.req("/reservation/end?id=18&lat=23.01&lng=19.01", (422, "payment_declined"), "declined", fault="decline",
          check=lambda b: b.get("ride_ended") is True, check_why="the body says the ride ended")
    d.expect_available("18", True, "a declined ride still frees the scooter")
    d.expect_db("18", lambda s: [b["reason"] for b in open_balances(s)] == ["payment_declined"],
                "18 owes one payment_declined balance")
    d.req("/reservation/start?id=18", 200, "18 can be reserved again")
    d.req("/reservation/end?id=18&lat=23&lng=19", 200, "and that ride is paid")


def concurrent(d):
    """8 riders race for 18 (one wins), then 4 end it at once (one ends it)."""
    starts = d.concurrent(["/reservation/start?id=18"] * 8, "8 concurrent starts of 18")
    d.check(sum(s == 200 for s in starts) == 1, "exactly one concurrent start wins", starts)
    ends = d.concurrent(["/reservation/end?id=18&lat=23.01&lng=19.01"] * 4, "4 concurrent ends of 18")
    d.check(sum(s == 200 for s in ends) == 1, "exactly one concurrent end ends it", ends)
    d.expect_available("18", True, "18 is free after the races")
    d.expect_ledger(lambda entries: sum(e["charged"] for e in entries) == 1, "the ride was charged once")


def restore(d):
    """DB rollbacks: a replayed ride isn't charged twice; an erased balance is rebuilt."""
    d.req("/reservation/start?id=18", 200, "start 18")
    d.snapshot("riding")
    d.req("/reservation/end?id=18&lat=23.01&lng=19.01", 200, "end 18 (paid)")
    d.restore("riding", "roll the DB back: 18 is riding again (the same reservation and key)")
    d.req("/reservation/end?id=18&lat=23.01&lng=19.01", 200, "ending it the same way returns the original charge")
    d.expect_ledger(lambda entries: sum(e["charged"] for e in entries) == 1, "still one charge: no double charge")
    d.snapshot("before-9")
    d.req("/reservation/start?id=9", 200, "start 9")
    d.req("/reservation/end?id=9&lat=-14.01&lng=-153.01", (422, "payment_declined"), "9 is declined", fault="decline")
    d.restore("before-9", "roll back past 9's ride: its balance is erased (the ledger keeps the decline)")
    d.reconcile("reconcile rebuilds the lost balance from the ledger")
    d.expect_db("9", lambda s: [b["reason"] for b in open_balances(s)] == ["rebuilt_from_ledger"],
                "9 owes the rebuilt balance")


def reconcile_stuck(d):
    """Stuck rides: a charged timeout settles; N failed attempts end unpaid (no N+1th call); a
    fresh failure is retried; an old one ends unpaid by age."""
    d.req("/reservation/start?id=15", 200, "start 15")
    d.req("/reservation/end?id=15&lat=0.01&lng=-129.01", (504, "payment_timeout"), "15 was charged, then timed out",
          fault="timeout_charged")
    d.req("/reservation/start?id=9", 200, "start 9")
    for n in (1, 2, 3):
        d.req("/reservation/end?id=9&lat=-14.01&lng=-153.01", (502, "payment_failed"), f"9's payment fails ({n} of 3)",
              fault="error")
    d.req("/reservation/start?id=18", 200, "start 18")
    d.req("/reservation/end?id=18&lat=23.01&lng=19.01", (502, "payment_failed"), "18's payment fails once", fault="error")
    d.reconcile("reconcile now (N=3 attempts, T=15 min)",
                expect={"15": "settle_charged", "9": "end_unpaid", "18": "retry"})
    d.expect_db("15", lambda s: not s["is_reserved"] and not open_balances(s), "15 settled with its charge")
    d.expect_db("9", lambda s: not s["is_reserved"] and [b["reason"] for b in open_balances(s)] == ["payment_unresolved"],
                "after 3 failed attempts 9 ended unpaid with a payment_unresolved balance (H3)")
    d.expect_ledger(lambda entries: sum(e["scooter_id"] == "9" for e in entries) == 3,
                    "reconcile made no 4th gateway call for 9")
    d.expect_db("18", lambda s: not s["is_reserved"] and not open_balances(s), "18's retry paid")
    d.req("/reservation/start?id=14", 200, "start 14")
    d.req("/reservation/end?id=14&lat=0.01&lng=0.01", (502, "payment_failed"), "14's payment fails once", fault="error")
    d.reconcile("reconcile an hour later: the age rule (T) ends 14 unpaid", hours_later=1, expect={"14": "end_unpaid"})
    d.expect_db("14", lambda s: not s["is_reserved"] and [b["reason"] for b in open_balances(s)] == ["payment_unresolved"],
                "14 ended unpaid by age")


SCENARIOS = {"rides": rides, "errors": errors, "payment-faults": payment_faults, "retry-settle": retry_settle,
             "decline": decline, "concurrent": concurrent, "restore": restore, "reconcile": reconcile_stuck}


def open_balances(scooter):
    return [b for b in scooter.get("outstanding_balances") or [] if "settled_by_txn_id" not in b]


# ---- the child: drives the working-tree app through its test client -------------------------------

class Driver:
    def __init__(self, name, out):
        import app
        self.app, self.name, self.out, self.steps = app, name, Path(out), []
        self.client = app.app.test_client()
        self.lock = threading.Lock()

    def _step(self, **step):
        with self.lock:
            step["n"] = len(self.steps) + 1
            self.steps.append(step)
        return step

    def _request(self, url, fault=None, method="GET", client=None):
        with self.lock:
            rid = f"scn-{self.name}-{len(self.steps) + 1:03d}"
        headers = {"X-Request-ID": rid}
        if fault:
            headers["X-Payment-Fault"] = fault
        resp = (client or self.client).open(url, method=method, headers=headers)
        body = resp.get_json(silent=True)
        kind = (body or {}).get("error", {}).get("kind") if isinstance(body, dict) else None
        return rid, resp.status_code, kind, body

    def req(self, url, expect, why, fault=None, method="GET", check=None, check_why=None):
        rid, status, kind, body = self._request(url, fault, method)
        want_status, want_kind = expect if isinstance(expect, tuple) else (expect, None)
        ok = status == want_status and (want_kind is None or kind == want_kind)
        problem = None if ok else f"expected {want_status}{' ' + want_kind if want_kind else ''}, got {status} {kind or ''}".strip()
        if ok and check is not None:
            try:
                ok = bool(check(body))
            except Exception as exc:           # a body of an unexpected shape
                ok = False
                problem = f"{check_why}: {type(exc).__name__}"
            if not ok and problem is None:
                problem = f"expected: {check_why}"
        return self._step(kind="request", request_id=rid, request=f"{method} {url}", fault=fault, status=status,
                          error_kind=kind, why=why, ok=ok, problem=problem,
                          body=body if isinstance(body, dict) else None)

    def check(self, ok, why, detail=None):
        return self._step(kind="check", why=why, ok=bool(ok), problem=None if ok else f"expected: {why}", detail=detail)

    def db(self):
        with open(os.environ["SCOOTER_DB_PATH"]) as f:
            return {s["id"]: s for s in json.load(f)}

    def expect_db(self, sid, predicate, why):
        scooter = self.db().get(sid)
        try:
            ok = scooter is not None and bool(predicate(scooter))
        except Exception:
            ok = False
        return self._step(kind="check", why=why, ok=ok, problem=None if ok else f"expected: {why}",
                          detail={"db": scooter})

    def expect_ledger(self, predicate, why):
        path = os.environ["SCOOTER_PAYMENTS_LEDGER_PATH"]
        entries = [json.loads(line) for line in open(path)] if os.path.exists(path) else []
        ok = bool(predicate(entries))
        return self._step(kind="check", why=why, ok=ok, problem=None if ok else f"expected: {why}",
                          detail={"ledger": [{k: e.get(k) for k in ("outcome", "charged", "scooter_id", "request_id")}
                                             for e in entries]})

    def expect_available(self, sid, yes, why):
        rid, status, kind, body = self._request("/view_all_available")
        listed = status == 200 and isinstance(body, list) and sid in [s.get("id") for s in body]
        ok = listed is yes
        return self._step(kind="request", request_id=rid, request="GET /view_all_available", status=status,
                          error_kind=kind, why=why, ok=ok,
                          problem=None if ok else f"expected scooter {sid} {'listed' if yes else 'not listed'}, "
                                                  f"but it {'is' if listed else 'is not'}")

    def expect_found(self, sid, yes, why):
        """Search right around the scooter's current position: is it in the results?"""
        s = self.db()[sid]
        url = f"/search?lat={s['lat']}&lng={s['lng']}&radius=1000"
        rid, status, kind, body = self._request(url)
        found = status == 200 and isinstance(body, list) and sid in [r.get("id") for r in body]
        ok = found is yes
        return self._step(kind="request", request_id=rid, request=f"GET {url}", status=status, error_kind=kind,
                          why=why, ok=ok, problem=None if ok else f"expected scooter {sid} {'found' if yes else 'not found'}, "
                                                                  f"but it {'is' if found else 'is not'}")

    def concurrent(self, urls, why):
        results = [None] * len(urls)
        barrier = threading.Barrier(len(urls))

        def go(i, url):
            client = self.app.app.test_client()
            barrier.wait()
            results[i] = self._request(url, client=client)
        threads = [threading.Thread(target=go, args=(i, u)) for i, u in enumerate(urls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self._step(kind="concurrent", why=why, ok=True, problem=None,
                   detail=[{"request_id": r[0], "status": r[1], "error_kind": r[2]} for r in results])
        return [r[1] for r in results]

    def snapshot(self, name):
        shutil.copyfile(os.environ["SCOOTER_DB_PATH"], self.out / f"snapshot-{name}.json")
        return self._step(kind="snapshot", why=f"snapshot the DB as {name!r}", ok=True, problem=None)

    def restore(self, name, why):
        shutil.copyfile(self.out / f"snapshot-{name}.json", os.environ["SCOOTER_DB_PATH"])
        return self._step(kind="restore", why=why, ok=True, problem=None)

    def reconcile(self, why, hours_later=0, expect=None):
        """One reconcile run; expect: {scooter_id: the action reconcile must take for it}."""
        import reconcile
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours_later)
        summary = reconcile.run(now=lambda: later)
        actions = {r.get("scooter_id"): r.get("action") for r in summary["results"]}
        problems = [f"scooter {sid}: expected {want}, reconcile did {actions.get(sid, 'nothing')}"
                    for sid, want in (expect or {}).items() if actions.get(sid) != want]
        if reconcile.remaining(summary):
            problems.append("reconcile left something (exit 1)")
        return self._step(kind="reconcile", why=why, ok=not problems, problem="; ".join(problems) or None,
                          detail=[{k: r.get(k) for k in ("scooter_id", "action", "result", "reason", "request_id")}
                                  for r in summary["results"]])


def child_main(name, out):
    driver = Driver(name, out)
    try:
        SCENARIOS[name](driver)
    except Exception as exc:                    # the app crashed the driver itself: that's evidence too
        driver._step(kind="crash", why="the scenario stopped", ok=False,
                     problem=f"{type(exc).__name__}: {exc}")
    Path(out, FILES["steps"]).write_text(json.dumps(driver.steps, indent=1))


# ---- the parent: prepare, run, analyse, print --------------------------------------------------

def committed_db():
    out = subprocess.run(["git", "show", "HEAD:scooter_db.json"], cwd=REPO, capture_output=True, text=True)
    return out.stdout if out.returncode == 0 and out.stdout.strip() else SEED_DB.read_text()


def prepare(out, live_paths):
    """A fresh directory for one run; an earlier run there is moved aside, never deleted."""
    out = Path(out).resolve()
    paths = {k: out / v for k, v in FILES.items()}
    if any(str(paths[k]) in live_paths for k in ("db", "records", "ledger")):     # before touching anything
        raise SystemExit("scenario: refusing to run where a live data file is (pick another --out)")
    if out.exists():
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        out.rename(out.with_name(f"{out.name}.{stamp}"))
    out.mkdir(parents=True)
    paths["db"].write_text(committed_db())
    return paths


def live_data_paths():
    db = os.environ.get("SCOOTER_DB_PATH") or "scooter_db.json"
    records = os.environ.get("SCOOTER_RECORDS_PATH") or os.path.join("data", "records.jsonl")
    ledger = os.environ.get("SCOOTER_PAYMENTS_LEDGER_PATH") or os.path.join("data", "payments_ledger.jsonl")
    return {str((REPO / p).resolve()) if not os.path.isabs(p) else str(Path(p).resolve()) for p in (db, records, ledger)}


def run_one(name, out):
    paths = prepare(out, live_data_paths())
    initial = json.loads(paths["db"].read_text())
    with tempfile.TemporaryDirectory(prefix="scenario-pycache-") as pycache:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SCOOTER_", "PAYMENT_", "RECONCILE_", "PYTHON")) and k != "ENABLE_FAULT_INJECTION"}
        env.update(SCOOTER_DB_PATH=str(paths["db"]), SCOOTER_RECORDS_PATH=str(paths["records"]),
                   SCOOTER_PAYMENTS_LEDGER_PATH=str(paths["ledger"]), ENABLE_FAULT_INJECTION="1",
                   PAYMENT_FAULT="none", PAYMENT_TIMEOUT_DELAY_MS="0",
                   PYTHONPYCACHEPREFIX=pycache, PYTHONDONTWRITEBYTECODE="1")   # fresh bytecode: the working tree
        proc = subprocess.run([sys.executable, "-B", str(REPO / "scenarios.py"), "--child", name, str(paths["db"].parent)],
                              cwd=paths["db"].parent, env=env, capture_output=True, text=True, timeout=300)
    steps = json.loads(paths["steps"].read_text()) if paths["steps"].exists() else \
        [{"n": 1, "kind": "crash", "ok": False, "why": "the scenario didn't run", "problem": proc.stderr.strip()[-2000:]}]
    summary = analyse(name, paths, initial, steps)
    summary["server_log_tail"] = proc.stderr.strip().splitlines()[-20:]
    paths["summary"].write_text(json.dumps(summary, indent=1))
    return summary


def analyse(name, paths, initial, steps):
    import records_query as rq
    records = list(rq.read_records(paths["records"])) if paths["records"].exists() else []
    ledger = rq.read_ledger(paths["ledger"]) if paths["ledger"].exists() else []
    db = json.loads(paths["db"].read_text())
    spec = importlib.util.spec_from_file_location("scenario_invariants", REPO / "tests" / "invariants.py")
    inv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inv)
    violations = inv.violations(db, ledger, inv.started_keys(records, initial), inv.pending_amounts(records))
    found = rq.unresolved(rq.anomalies(records, db=db, db_path=str(paths["db"])))
    ledger_result = rq.ledger_check(ledger, records, min_age_s=0)
    unexpected = [s for s in steps if not s.get("ok")]
    dirty = next((r.get("git_dirty_paths") for r in records if r.get("git_dirty")), [])
    return {"scenario": name, "dir": str(paths["db"].parent), "requests": len(records),
            "unexpected": unexpected, "anomalies": [{"type": a["type"], "request_id": a["request_id"]} for a in found],
            "invariant_violations": [list(v) for v in violations],
            "ledger_problems": rq.ledger_problems(ledger_result),
            "failure_counts": rq.failure_counts(records), "git_dirty_paths": dirty,
            "clean": not (unexpected or found or violations or rq.ledger_problems(ledger_result))}


def report(summary):
    d = summary["dir"]
    rel = os.path.relpath(d, REPO) if d.startswith(str(REPO)) else d
    lines = [f"scenario {summary['scenario']}: {summary['requests']} requests, "
             f"{len(summary['unexpected'])} unexpected step(s), {len(summary['anomalies'])} anomaly(ies), "
             f"{len(summary['invariant_violations'])} invariant violation(s), "
             f"ledger-check {'PROBLEMS' if summary['ledger_problems'] else 'clean'}"]
    if summary["git_dirty_paths"]:
        lines.append(f"  NOTE: the code that ran differs from HEAD in: {', '.join(summary['git_dirty_paths'])}")
    for s in summary["unexpected"]:
        lines.append(f"  UNEXPECTED step {s['n']}: {s.get('why')}: {s.get('problem')}"
                     + (f"  [{s['request']}, request {s['request_id']}]" if s.get("request_id") else ""))
    for a in summary["anomalies"]:
        lines.append(f"  ANOMALY {a['type']} in request {a['request_id']}")
    for v in summary["invariant_violations"]:
        lines.append(f"  INVARIANT {v[0]}: {v[1]}")
    lines += [f"  evidence: {rel}/ (records.jsonl, db.json, ledger.jsonl, steps.json, summary.json)",
              "  query it:",
              f"    make records ARGS=\"--file {rel}/records.jsonl recent-failures\"",
              f"    make records ARGS=\"--file {rel}/records.jsonl scooter 18\"",
              f"    make records ARGS=\"--file {rel}/records.jsonl request <request_id>\"",
              f"    make records ARGS=\"--file {rel}/records.jsonl anomalies --db {rel}/db.json\"",
              f"    make records ARGS=\"--file {rel}/records.jsonl ledger-check --ledger {rel}/ledger.jsonl --min-age 0\"",
              f"    SCOOTER_DB_PATH={rel}/db.json SCOOTER_RECORDS_PATH={rel}/records.jsonl "
              f"SCOOTER_PAYMENTS_LEDGER_PATH={rel}/ledger.jsonl .venv/bin/python tests/invariants.py",
              f"    MCP: any read-only scooter tool with scenario=\"{summary['scenario']}\""
              + ("" if Path(d).parent.resolve() == Path(os.environ.get("SCOOTER_SCENARIOS_DIR") or DEFAULT_ROOT).resolve()
                 else " (only for runs in the default scenarios directory)")]
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--child"]:
        child_main(argv[1], argv[2])
        return 0
    parser = argparse.ArgumentParser(prog="make scenario NAME=", description=__doc__.split("\n\n")[0])
    parser.add_argument("name", nargs="?", help=f"one of {', '.join(SCENARIOS)}, or all")
    parser.add_argument("--out", help="the run's directory (default data/scenarios/<name>/)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    if not args.name:
        print("scenarios (make scenario NAME=<name>, or NAME=all):")
        for name, fn in SCENARIOS.items():
            print(f"  {name:15} {fn.__doc__.strip()}")
        return 0
    names = list(SCENARIOS) if args.name == "all" else [args.name]
    if any(n not in SCENARIOS for n in names):
        parser.error(f"unknown scenario {args.name!r}; one of {', '.join(SCENARIOS)}, or all")
    if args.out and len(names) > 1:
        parser.error("--out takes one scenario")
    root = Path(os.environ.get("SCOOTER_SCENARIOS_DIR") or DEFAULT_ROOT)
    summaries = [run_one(n, args.out or root / n) for n in names]
    if args.json:
        print(json.dumps(summaries if len(summaries) > 1 else summaries[0]))
    else:
        print("\n\n".join(report(s) for s in summaries))
    return 0 if all(s["clean"] for s in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
