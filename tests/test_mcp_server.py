"""The MCP server (PLAN PR 16; ACTIVITY Outcome 4), through a real MCP client session.

Most tests use the SDK's in-process transport (`mcp.Client(server)`); one runs the server
as a stdio subprocess, the way `.mcp.json` does. The data files are the test's own (the
autouse fixtures in conftest), so every tool reads fixture data, and the conftest guards
fail a test that touches the live files.
"""
import json
import os
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

import mcp_server
import payments

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("PAYMENT_TIMEOUT_DELAY_MS", "0")


def call(name, args=None, server=None):
    """One tool call in a fresh client session -> CallToolResult."""
    async def go():
        async with Client(server or mcp_server.server) as client:
            return await client.call_tool(name, args or {})
    return anyio.run(go)


def ok(name, args=None):
    res = call(name, args)
    assert not res.is_error, res.content[0].text
    return res.structured_content


def err(name, args=None):
    res = call(name, args)
    assert res.is_error, res.structured_content
    return res.content[0].text


@pytest.fixture
def history(client, monkeypatch):
    """18: a charged timeout (payment_pending); 15: declined (ended with a balance); a 404."""
    ids = {}
    ids["start18"] = client.get("/reservation/start?id=18").headers["X-Request-ID"]
    client.get("/reservation/start?id=18")                                   # refused: already reserved
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_charged")
    ids["end18"] = client.get("/reservation/end?id=18&lat=1&lng=1").headers["X-Request-ID"]
    client.get("/reservation/start?id=15")
    monkeypatch.setenv("PAYMENT_FAULT", "decline")
    ids["end15"] = client.get("/reservation/end?id=15&lat=2&lng=2").headers["X-Request-ID"]
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    client.get("/nope")
    ids["end_free"] = client.get("/reservation/end?id=9&lat=2&lng=2").headers["X-Request-ID"]    # 422: not reserved
    return ids


# ---- listing ---------------------------------------------------------------------------------

def test_the_tools_are_listed_with_descriptions_and_schemas():
    async def go():
        async with Client(mcp_server.server) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == set(mcp_server.READ_ONLY_TOOLS) | set(mcp_server.ACTION_TOOLS)
    assert not set(mcp_server.READ_ONLY_TOOLS) & set(mcp_server.ACTION_TOOLS)
    for t in tools.values():
        assert t.description and t.input_schema["type"] == "object", t.name
    assert tools["scooter_history"].input_schema["required"] == ["scooter_id"]


# ---- each read-only tool on fixture data ----------------------------------------------------

def test_what_failed_recently(history, records_path):
    out = ok("recent_failures")
    kinds = [r["error"]["kind"] for r in out["result"]]
    assert set(kinds) == {"payment_timeout", "payment_declined", "scooter_already_reserved", "route_not_found",
                         "no_active_reservation"}
    assert out["files_read"]["records"]["path"] == str(records_path.resolve()) and out["files_read"]["records"]["records"] == 7
    assert len(ok("recent_failures", {"limit": 1})["result"]) == 1
    assert ok("recent_failures", {"since": "2999-01-01T00:00:00Z"})["result"] == []
    counts = ok("failure_counts", {"since": "1h"})["result"]
    assert counts["payment_timeout"] == 1 and counts["route_not_found"] == 1


def test_what_happened_to_scooter_18(history):
    out = ok("scooter_history", {"scooter_id": "18"})["result"]
    assert [(r["path"], r["status"]) for r in out] == [("/reservation/start", 200), ("/reservation/start", 422),
                                                      ("/reservation/end", 504)]
    [trace] = ok("request_trace", {"request_id": history["end18"]})["result"]
    assert ("payment", "failed") in [(d["step"], d["outcome"]) for d in trace["decisions"]]
    state = ok("scooter_state", {"scooter_id": "18"})
    assert state["result"][0]["reservation"]["payment_state"] == "payment_pending"
    assert len(ok("scooter_state")["result"]) == 5
    assert ok("scooter_state", {"scooter_id": "15"})["result"][0]["outstanding_balances"][0]["reason"] == "payment_declined"


def test_cross_checks_on_fixture_data(history, db_path, ledger_path):
    out = ok("anomalies")
    assert out["result"]["unresolved"] == 0 and out["files_read"]["db"]["path"] == str(db_path.resolve())
    assert ok("ledger_check", {"min_age_s": 0})["result"]["problems"] is False
    assert ok("rollback_check")["result"]["problems"] is False
    inv = ok("check_invariants")
    assert inv["result"] == {"violations": [], "ok": True}
    assert set(inv["files_read"]) == {"records", "db", "ledger"}
    assert inv["files_read"]["ledger"]["path"] == str(ledger_path.resolve())
    db_path.write_text((REPO / "tests" / "seed_db.json").read_text())       # a whole-DB rollback
    assert ok("rollback_check")["result"]["erased_rides"] == [] and ok("rollback_check")["result"]["problems"]
    assert not ok("check_invariants")["result"]["ok"]


def test_payment_tools(history, read_ledger):
    key = read_ledger()[0]["idempotency_key"]
    out = ok("payment_lookup", {"idempotency_key": key})
    assert [e["outcome"] for e in out["result"]] == ["timeout_charged"]
    assert ok("payment_lookup", {"idempotency_key": "nope"})["result"] == []
    draw = ok("payment_draw", {"seed": "42", "idempotency_key": "legacy-33", "attempt": 1, "rate": 0.7})["result"]
    outcome, u = payments.draw("42", "legacy-33", 1, 0.7)
    assert (draw["outcome"], draw["draw"]) == (outcome, round(u, 6))


def test_reconcile_plan_changes_nothing_and_reconcile_run_settles(history, db_path, read_records):
    before, n = db_path.read_bytes(), len(read_records())
    plan = ok("reconcile_plan")["result"]
    assert [(r["scooter_id"], r["action"], r["result"]) for r in plan["results"]] == [("18", "settle_charged", "dry_run")]
    assert db_path.read_bytes() == before and len(read_records()) == n
    run = ok("reconcile_run")["result"]
    assert [(r["action"], r["result"]) for r in run["results"]] == [("settle_charged", "ended_paid")]
    assert run["remaining"] is False and ok("scooter_state", {"scooter_id": "18"})["result"][0]["reservation"] is None


# ---- errors are tool errors, and the server keeps answering ----------------------------------

@pytest.mark.parametrize("name,args,says", [
    ("request_trace", {"request_id": "nope"}, "no record with request_id"),
    ("scooter_state", {"scooter_id": "nope"}, "no scooter with id"),
    ("recent_failures", {"since": "yesterday"}, "--since must be"),
    ("recent_failures", {"limit": 0}, "limit must be"),
    ("payment_draw", {"seed": "s", "idempotency_key": "k", "attempt": 0}, "attempt must be"),
    ("reconcile_plan", {"max_attempts": 0}, "max_attempts must be"),
    ("scooter_history", {}, "scooter_id"),                                   # a missing argument
    ("no_such_tool", {}, "no_such_tool"),
])
def test_bad_calls_are_tool_errors(name, args, says):
    assert says in err(name, args)
    assert ok("failure_counts")["result"] == {}                              # still answering


def test_unusable_files_are_tool_errors(db_path, ledger_path):
    ledger_path.write_text('{"idempotency_key": "k"\n')
    assert "ledger unreadable" in err("payment_lookup", {"idempotency_key": "k"})
    assert "ledger unreadable" in err("check_invariants")
    db_path.write_text("not json")
    assert "the DB is unusable" in err("scooter_state")
    assert "cannot read the DB" in err("rollback_check")


def test_a_missing_records_file_means_no_records_yet(records_path):
    assert not records_path.exists()
    out = ok("recent_failures")
    assert out["result"] == [] and out["files_read"]["records"]["exists"] is False


# ---- replay_fault: reproduce, on an isolated instance only -----------------------------------

def _snapshot(*paths):
    return {p: (p.read_bytes() if p.exists() else None) for p in paths}


def test_replay_reproduces_an_env_fault_without_touching_the_files(history, db_path, records_path, ledger_path):
    before = _snapshot(db_path, records_path, ledger_path)
    out = ok("replay_fault", {"request_id": history["end18"]})["result"]
    assert out["reproduced"] is True
    assert (out["replayed"]["status"], out["replayed"]["gateway_outcome"]) == (504, "timeout_charged")
    assert out["replayed"]["request_id"] == f"replay-{history['end18']}"
    assert out["fault_env"]["PAYMENT_FAULT"] == "timeout_charged"
    assert [e["outcome"] for e in out["replayed_ledger_entries"]] == ["timeout_charged"]
    assert _snapshot(db_path, records_path, ledger_path) == before          # the guard: nothing written


def test_replay_reproduces_a_decline_and_can_ask_what_if(history):
    out = ok("replay_fault", {"request_id": history["end15"]})["result"]
    assert out["reproduced"] is True and out["replayed"]["error_kind"] == "payment_declined"
    what_if = ok("replay_fault", {"request_id": history["end15"], "fault": "none"})["result"]
    assert what_if["reproduced"] is None and what_if["replayed"]["status"] == 200


def test_replay_reproduces_a_seeded_retry_with_its_earlier_attempts(client, monkeypatch):
    client.get("/reservation/start?id=18")
    monkeypatch.setenv("PAYMENT_FAULT", "error")                          # attempt 1: the ride stays pending
    assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == 502
    monkeypatch.setenv("PAYMENT_FAULT", "random")
    monkeypatch.setenv("PAYMENT_SEED", "7")
    monkeypatch.setenv("PAYMENT_FAULT_RATE", "1")                         # attempt 2: a seeded fault (any of four)
    rid = client.get("/reservation/end?id=18&lat=1&lng=1").headers["X-Request-ID"]
    for name in ("PAYMENT_FAULT", "PAYMENT_SEED", "PAYMENT_FAULT_RATE"):
        monkeypatch.delenv(name)                                          # the replay must not need them
    out = ok("replay_fault", {"request_id": rid})["result"]
    assert out["earlier_ledger_entries"] == 1 and out["reproduced"] is True
    assert out["replayed"]["fault"]["attempt"] == 2 and out["replayed"]["fault"]["seed"] == "7"
    assert out["fault_env"] == {"PAYMENT_TIMEOUT_DELAY_MS": "0", "PAYMENT_SEED": "7", "PAYMENT_FAULT_RATE": "1.0",
                                "PAYMENT_FAULT": "random"}


def test_replay_reproduces_a_header_fault(client, monkeypatch):
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    client.get("/reservation/start?id=18")
    rid = client.get("/reservation/end?id=18&lat=1&lng=1", headers={"X-Payment-Fault": "error"}).headers["X-Request-ID"]
    monkeypatch.delenv("ENABLE_FAULT_INJECTION")
    out = ok("replay_fault", {"request_id": rid})["result"]
    assert out["reproduced"] is True and out["fault_header"] == "error" and out["replayed"]["status"] == 502


def test_replay_refuses_what_it_cant_replay(history, monkeypatch, db_path):
    assert "never called the payment gateway" in err("replay_fault", {"request_id": history["end_free"]})
    assert "no /reservation/end record" in err("replay_fault", {"request_id": "nope"})
    assert "fault must be one of" in err("replay_fault", {"request_id": history["end18"], "fault": "explode"})
    # the guard itself: a temp dir that is where the live DB lives is refused
    import tempfile

    class LiveDir:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return str(db_path.parent)

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(tempfile, "TemporaryDirectory", LiveDir)
    db_path.rename(db_path.parent / "db.json")
    monkeypatch.setenv("SCOOTER_DB_PATH", str(db_path.parent / "db.json"))
    assert "would include a live data file" in err("replay_fault", {"request_id": history["end18"]})


# ---- run_tests: validated arguments, bounded output --------------------------------------------

@pytest.mark.parametrize("args,says", [
    ({"paths": ["tests/../app.py"]}, "isn't a test file"),
    ({"paths": ["app.py"]}, "isn't a test file"),
    ({"paths": ["tests/test_nope.py"]}, "isn't a test file"),
    ({"paths": ["-p", "tests/test_view_all.py"]}, "isn't a test file"),
    ({"paths": ["tests/test_view_all.py --co"]}, "isn't a test file"),
    ({"paths": [str(REPO / "tests" / "test_view_all.py")]}, "isn't a test file"),          # absolute
    ({"paths": ["./tests/test_view_all.py"]}, "isn't a test file"),
    ({"keyword": "--collect-only"}, "keyword:"),
    ({"keyword": "x; rm -rf /"}, "keyword:"),
    ({"keyword": "a" * 201}, "keyword:"),
    ({"timeout_s": 0}, "timeout_s must be"),
    ({"timeout_s": 10_000}, "timeout_s must be"),
    ({"tail_lines": 1000}, "tail_lines"),
])
def test_run_tests_rejects_anything_but_the_validated_subset(args, says):
    assert says in err("run_tests", args)


def test_run_tests_builds_only_pytest_argv():
    argv = mcp_server.pytest_argv(["tests/test_view_all.py", "tests/test_search.py::test_search_radius"],
                                  "search and not slow", True)
    assert argv == [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_view_all.py",
                    "tests/test_search.py::test_search_radius", "-k", "search and not slow", "-x"]


def test_run_tests_runs_a_subset_and_returns_a_bounded_tail(monkeypatch):
    monkeypatch.delenv(mcp_server.NESTED, raising=False)
    out = ok("run_tests", {"paths": ["tests/test_view_all.py"], "tail_lines": 3})
    assert out["passed"] is True and out["exit_code"] == 0 and "passed" in out["summary"]
    assert len(out["tail"]) <= 3 and out["command"].startswith("-m pytest")


def test_run_tests_ignores_pytest_and_python_variables(monkeypatch, tmp_path):
    # PR #23 review: PYTEST_ADDOPTS="--co -q" would turn the run into a collection-only "pass"
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "sitecustomize.py").write_text("raise SystemExit('sitecustomize from PYTHONPATH ran')\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--co -q")
    monkeypatch.setenv("PYTEST_PLUGINS", "no_such_plugin")
    monkeypatch.setenv("PYTHONPATH", str(evil))
    monkeypatch.setenv("PYTHONSTARTUP", str(evil / "sitecustomize.py"))
    env = mcp_server.child_env()
    assert not [k for k in env if k.startswith(("PYTEST_", "PYTHONPATH", "PYTHONSTARTUP"))]
    assert env[mcp_server.NESTED] == "1" and env["SCOOTER_DB_PATH"] == os.environ["SCOOTER_DB_PATH"]
    out = ok("run_tests", {"paths": ["tests/test_view_all.py"]})
    assert out["passed"] is True and " passed" in out["summary"] and "collected" not in out["summary"]


def test_run_tests_times_out_and_refuses_nesting(monkeypatch):
    monkeypatch.setattr(mcp_server, "pytest_argv", lambda *a: [sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    out = ok("run_tests", {"timeout_s": 1})
    assert out["timed_out"] is True and out["passed"] is False and time.monotonic() - started < 30
    monkeypatch.setattr(mcp_server, "pytest_argv",
                        lambda *a: [sys.executable, "-c", "print('\\n'.join(f'line {i}' for i in range(1000)))"])
    out = ok("run_tests", {"tail_lines": 5})
    assert out["tail"] == [f"line {i}" for i in range(995, 1000)] and out["summary"] == "line 999"
    monkeypatch.setenv(mcp_server.NESTED, "1")
    assert "already running" in err("run_tests")


# ---- end to end over stdio, as .mcp.json runs it -------------------------------------------------

def test_the_server_over_stdio(history, records_path):
    params = StdioServerParameters(command=sys.executable, args=[str(REPO / "mcp_server.py")],
                                   env=dict(os.environ), cwd=str(REPO))

    async def go():
        async with Client(params) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            res = await client.call_tool("failure_counts", {})
            bad = await client.call_tool("request_trace", {"request_id": "nope"})
            return names, res, bad
    names, res, bad = anyio.run(go)
    assert "run_tests" in names and not res.is_error and bad.is_error
    assert res.structured_content["result"]["payment_declined"] == 1
    assert res.structured_content["files_read"]["records"]["path"] == str(records_path.resolve())


def test_mcp_json_registers_this_server():
    config = json.loads((REPO / ".mcp.json").read_text())["mcpServers"][mcp_server.NAME]
    assert (config["command"], config["args"]) == (".venv/bin/python", ["mcp_server.py"])
