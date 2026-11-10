"""The cold-agent playbook works (PLAN PR 17; ACTIVITY "the acceptance test").

A planted bug in the working tree must show up in `make scenario`'s evidence: UNEXPECTED
steps, anomalies, invariant violations, and records that locate the bad code path. Each
test plants a bug in a throwaway copy of the code, runs the scenario the AGENTS.md
playbook says to run, and asserts the evidence names it. The same scenarios on the real
code are clean. Also: `make reset-local` (deletes nothing) and the MCP tools' `scenario=`.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client

import mcp_server
import records_query as rq

REPO = Path(__file__).resolve().parent.parent
CODE = ["app.py", "records.py", "failures.py", "payments.py", "reconcile.py", "records_query.py", "scenarios.py",
        "tests/invariants.py", "tests/seed_db.json"]


@pytest.fixture
def scenarios_dir(tmp_path, monkeypatch):
    path = tmp_path / "scenarios"
    monkeypatch.setenv("SCOOTER_SCENARIOS_DIR", str(path))
    return path


def run_scenario(name, repo=REPO, extra=()):
    out = subprocess.run([sys.executable, str(repo / "scenarios.py"), name, "--json", *extra], cwd=repo,
                         capture_output=True, text=True, timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert out.stdout, out.stderr
    return out.returncode, json.loads(out.stdout)


def planted_copy(tmp_path, file, old, new):
    copy = tmp_path / "planted"
    for rel in CODE:
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, copy / rel)
    source = (copy / file).read_text()
    assert source.count(old) == 1, f"{file} changed shape; update this plant"
    (copy / file).write_text(source.replace(old, new))
    return copy


def records_of(summary):
    return list(rq.read_records(Path(summary["dir"]) / "records.jsonl"))


# ---- the scenarios are clean on the real code ---------------------------------------------------

def test_every_scenario_is_clean_on_the_real_code_by_every_documented_command(scenarios_dir):
    code, summaries = run_scenario("all")
    bad = {s["scenario"]: (s["unexpected"], s["anomalies"], s["invariant_violations"]) for s in summaries if not s["clean"]}
    assert code == 0 and bad == {}
    assert {s["scenario"] for s in summaries} == {"rides", "errors", "payment-faults", "retry-settle", "decline",
                                                  "concurrent", "restore", "reconcile"}
    assert all(Path(s["dir"]).parent == scenarios_dir for s in summaries)
    assert sum(s["requests"] for s in summaries) > 60
    # PR #24 review: the playbook's own step-4 commands must agree with the summary on clean
    # data (the invariants CLI reported a false I3 for legacy-33 on every clean `rides` run)
    for s in summaries:
        d = Path(s["dir"])
        env = dict(os.environ, SCOOTER_DB_PATH=str(d / "db.json"), SCOOTER_RECORDS_PATH=str(d / "records.jsonl"),
                   SCOOTER_PAYMENTS_LEDGER_PATH=str(d / "ledger.jsonl"))
        invariants = subprocess.run([sys.executable, "tests/invariants.py"], cwd=REPO, env=env, capture_output=True, text=True)
        assert invariants.returncode == 0, (s["scenario"], invariants.stdout)
        for args in (["anomalies", "--db", str(d / "db.json")],
                     ["ledger-check", "--ledger", str(d / "ledger.jsonl"), "--min-age", "0"]):
            cli = subprocess.run([sys.executable, "-m", "records_query", "--file", str(d / "records.jsonl"), *args],
                                 cwd=REPO, capture_output=True, text=True)
            assert cli.returncode == 0, (s["scenario"], args[0], cli.stdout)
        mcp_invariants = _call("check_invariants", {"scenario": s["scenario"]})
        assert mcp_invariants.structured_content["result"]["ok"], (s["scenario"], mcp_invariants.structured_content)
        assert _call("anomalies", {"scenario": s["scenario"]}).structured_content["result"]["unresolved"] == 0


def test_a_scenario_never_touches_the_live_files_and_keeps_earlier_runs(scenarios_dir):
    run_scenario("decline")
    run_scenario("decline")                            # the first run is moved aside, not deleted
    runs = sorted(p.name for p in scenarios_dir.iterdir())
    assert runs[0] == "decline" and len(runs) == 2 and runs[1].startswith("decline.")
    # (the conftest guards fail this test if the live scooter_db.json / data/ files changed)


def test_a_scenario_refuses_to_run_where_a_live_file_is(tmp_path, monkeypatch, scenarios_dir):
    live = tmp_path / "guarded"
    live.mkdir()
    (live / "db.json").write_text("LIVE")
    monkeypatch.setenv("SCOOTER_DB_PATH", str(live / "db.json"))
    out = subprocess.run([sys.executable, str(REPO / "scenarios.py"), "rides", "--out", str(live)], cwd=REPO,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode != 0 and "refusing to run where a live data file is" in out.stderr
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("guarded")) == ["guarded"]
    assert (live / "db.json").read_text() == "LIVE"


def test_an_anomaly_alone_makes_a_run_unclean(tmp_path):
    spec = importlib.util.spec_from_file_location("scenarios_mod", REPO / "scenarios.py")
    scenarios = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scenarios)
    db = json.loads((REPO / "tests" / "seed_db.json").read_text())
    paths = {k: tmp_path / v for k, v in scenarios.FILES.items()}
    paths["db"].write_text(json.dumps(db))
    free, reserved = dict(db[1], is_reserved=False), dict(db[1], is_reserved=True)
    record = {"ts": "2026-11-10T00:00:00.000Z", "request_id": "r", "path": "/reservation/start", "status": 400,
              "scooter_id": "18", "decisions": [], "db_write": {"attempted": True, "ok": True, "error": None},
              "scooter_before": free, "scooter_after": free, "scooter_in_memory": reserved}
    paths["records"].write_text(json.dumps(record) + "\n")
    summary = scenarios.analyse("x", paths, db, steps=[])
    assert summary["unexpected"] == [] and [a["type"] for a in summary["anomalies"]] == ["write_not_persisted"]
    assert summary["clean"] is False


def test_the_report_says_where_the_evidence_is_and_how_to_query_it(scenarios_dir):
    out = subprocess.run([sys.executable, str(REPO / "scenarios.py"), "rides"], cwd=REPO, capture_output=True,
                         text=True, timeout=300)
    assert out.returncode == 0
    for needle in ("scenario rides: 12 requests, 0 unexpected", "records.jsonl scooter 18", "anomalies --db",
                   "tests/invariants.py", 'scenario="rides"'):
        assert needle in out.stdout, needle
    listing = subprocess.run([sys.executable, str(REPO / "scenarios.py")], capture_output=True, text=True)
    assert "payment-faults" in listing.stdout and listing.returncode == 0


# ---- planted bugs: the playbook's scenario names each one -----------------------------------------

def test_the_activity_symptom_ended_but_still_reserved(tmp_path, scenarios_dir):
    """'I ended my ride on scooter 18 but it still shows as reserved': the end writes the DB
    before it clears the reservation (a persistence-order bug)."""
    copy = planted_copy(tmp_path, "app.py", "\t\t\t\t\tfinish_ride(scooter)\n\t\t\t\t\twrite_db(db, txn.path)\n",
                        "\t\t\t\t\twrite_db(db, txn.path)\n\t\t\t\t\tfinish_ride(scooter)\n")
    code, summary = run_scenario("rides", copy)
    assert code == 1
    whys = [s["why"] for s in summary["unexpected"]]
    assert "after its ride ended, 18 is listed again (the ACTIVITY symptom)" in whys
    assert {"write_not_persisted", "end_left_reserved"} <= {a["type"] for a in summary["anomalies"]}
    # the record locates the bad code path: what was written still has the ride, what the code
    # believed doesn't
    [end] = [r for r in records_of(summary) if r["path"] == "/reservation/end" and r["scooter_id"] == "18"]
    assert end["status"] == 200 and end["db_write"]["ok"] is True
    assert end["scooter_after"]["is_reserved"] is True and end["scooter_in_memory"]["is_reserved"] is False


def test_the_activity_symptom_finish_ride_keeps_the_flag(tmp_path, scenarios_dir):
    """Same symptom, another cause: finish_ride clears the reservation but not is_reserved,
    so the write guard refuses the invalid state after the ride was charged."""
    copy = planted_copy(tmp_path, "app.py", "\tscooter.reservation = None\n\tscooter.is_reserved = False\n",
                        "\tscooter.reservation = None\n")
    code, summary = run_scenario("rides", copy)
    assert code == 1
    [end] = [s for s in summary["unexpected"] if s["why"] == "end the ride on 18 (paid)"]
    assert end["problem"] == "expected 200, got 500 internal_error"
    assert "charged_without_persisted_end" in {a["type"] for a in summary["anomalies"]}
    [record] = [r for r in records_of(summary) if r["request_id"] == end["request_id"]]
    refused = [d for d in record["decisions"] if (d["step"], d["outcome"]) == ("db_write", "refused")]
    assert "is_reserved is true but reservation is null" in refused[0]["detail"]["error"]


def test_rehearsal_plant_swapped_lat_lng_prints_expected_vs_actual(tmp_path, scenarios_dir):
    """PLAN step 20, the cold-agent rehearsal: finish_ride swapped lat/lng. The agent found it,
    but the step said only "expected: 18 is free at (23.01, 19.01)" and the anomaly's name
    suggested a lost write. Now both name the actual values and the kind of bug (PLAN PR 20a)."""
    copy = planted_copy(tmp_path, "app.py", "\tscooter.lat, scooter.lng = end_lat, end_lng\n",
                        "\tscooter.lat, scooter.lng = end_lng, end_lat\n")
    code, summary = run_scenario("rides", copy)
    assert code == 1
    problems = {s["why"]: s["problem"] for s in summary["unexpected"]}
    assert problems["18 is free at (23.01, 19.01) in the DB"] == "expected at=(23.01, 19.01), got at=(19.01, 23.01)"
    assert problems["search near 18"] == "expected: the search finds 18; got []"
    [found] = [a for a in summary["anomalies"] if a["type"] == "end_location_not_persisted"
               and a["request_id"] == "scn-rides-004"]
    assert found["summary"].startswith("expected the scooter at (23.01, 19.01), it's at (19.01, 23.01) on disk; "
                                       "cause: logic")
    out = subprocess.run([sys.executable, str(copy / "scenarios.py"), "rides"], cwd=copy, capture_output=True, text=True,
                         timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert "UNEXPECTED step 6: 18 is free at (23.01, 19.01) in the DB: expected at=(23.01, 19.01), got at=(19.01, 23.01)" \
        in out.stdout, out.stdout
    assert "cause: logic" in out.stdout


def test_reviewer_plant_view_all_lists_pending_scooters(tmp_path, scenarios_dir):
    """PR #24 review, blind run 1: scenarios missed it; now a step and an anomaly name it."""
    copy = planted_copy(tmp_path, "app.py",
                        "\tavailable_scooters = [scooter for scooter in db if not scooter.is_reserved]",
                        "\tavailable_scooters = [scooter for scooter in db if not scooter.is_reserved"
                        " or scooter.reservation['payment_state'] == 'payment_pending']")
    code, summary = run_scenario("payment-faults", copy)
    assert code == 1
    assert "15 isn't listed while its payment is pending" in [s["why"] for s in summary["unexpected"]]
    assert "listed_unavailable" in {a["type"] for a in summary["anomalies"]}
    [view] = [r for r in records_of(summary) if r["request_id"] == next(
        s["request_id"] for s in summary["unexpected"] if s["why"] == "15 isn't listed while its payment is pending")]
    [listing] = [d for d in view["decisions"] if d["step"] == "filter"]
    assert "15" in listing["detail"]["listed"] and listing["detail"]["unavailable"]["15"] == "payment_pending"


def test_search_listing_reserved_scooters_is_named_too(tmp_path, scenarios_dir):
    copy = planted_copy(tmp_path, "app.py", "\t\tif distance <= search_radius and not scooter.is_reserved:",
                        "\t\tif distance <= search_radius:")
    code, summary = run_scenario("payment-faults", copy)
    assert code == 1
    assert "15 isn't found by search while its payment is pending" in [s["why"] for s in summary["unexpected"]]
    assert "listed_unavailable" in {a["type"] for a in summary["anomalies"]}


def test_reviewer_plant_reconcile_attempt_limit_off_by_one(tmp_path, scenarios_dir):
    """PR #24 review, blind run 2: the reconcile scenario only exercised the age rule."""
    copy = planted_copy(tmp_path, "reconcile.py", "        if attempts >= max_attempts:", "        if attempts > max_attempts:")
    code, summary = run_scenario("reconcile", copy)
    assert code == 1
    problems = [s["problem"] for s in summary["unexpected"]]
    assert "scooter 9: expected end_unpaid, reconcile did retry" in problems[0]
    assert "expected calls_for_9=3, got calls_for_9=4" in problems          # the actual count, not just the claim


def test_reviewer_plant_f2_a_new_reservation_and_key_per_attempt(tmp_path, scenarios_dir):
    """PR #24 re-review, F2: each failed attempt rotates BOTH reservation_id and key, so the
    retry charges again. The loosened started_keys hid it from I3 and ledger-check; the
    documented invariants command must name it."""
    copy = planted_copy(tmp_path, "app.py",
                        "\tscooter.reservation['payment_attempts'] += 1\n",
                        "\tscooter.reservation['payment_attempts'] += 1\n"
                        "\tscooter.reservation['reservation_id'] = scooter.reservation['idempotency_key'] = uuid.uuid4().hex\n")
    code, summary = run_scenario("retry-settle", copy)
    assert code == 1
    assert "the retry wasn't charged again" in [s["why"] for s in summary["unexpected"]]
    assert "scooter_double_charge" in {a["type"] for a in summary["anomalies"]}
    d = Path(summary["dir"])
    env = dict(os.environ, SCOOTER_DB_PATH=str(d / "db.json"), SCOOTER_RECORDS_PATH=str(d / "records.jsonl"),
               SCOOTER_PAYMENTS_LEDGER_PATH=str(d / "ledger.jsonl"))
    out = subprocess.run([sys.executable, "tests/invariants.py"], cwd=REPO, env=env, capture_output=True, text=True)
    assert out.returncode == 1 and "I3 charged under a key no reservation owned" in out.stdout
    assert any(v[0] == "I3 charged under a key no reservation owned" for v in summary["invariant_violations"])


def test_reviewer_plant_conflict_doesnt_reopen_the_ride(tmp_path, scenarios_dir):
    """PR #24 re-review, blind run 3: abandon_payment no longer resets the ride to active."""
    copy = planted_copy(tmp_path, "app.py", "\tscooter.reservation['payment_state'] = 'active'\n", "")
    code, summary = run_scenario("restore", copy)
    assert code == 1
    [bad] = [s for s in summary["unexpected"] if s["why"] == "ending the restored ride elsewhere is a conflict, not a second ride"]
    assert bad["problem"] == "expected 409 payment_conflict, got 500 internal_error"
    [record] = [r for r in records_of(summary) if r["request_id"] == bad["request_id"]]
    steps = [(d["step"], d["outcome"]) for d in record["decisions"]]
    assert ("reservation", "payment_abandoned") in steps and ("db_write", "refused") in steps


def test_a_payment_bug_timeout_ends_the_ride_as_paid(tmp_path, scenarios_dir):
    copy = planted_copy(tmp_path, "app.py", "\t\t\t\tif payment_response.ok or outcome == 'decline':",
                        "\t\t\t\tif payment_response.ok or outcome in ('decline', 'timeout_not_charged'):")
    code, summary = run_scenario("payment-faults", copy)
    assert code == 1
    assert any(s["why"] == "end 15 with timeout_not_charged" and "got 200" in s["problem"] for s in summary["unexpected"])
    assert "ended_without_payment" in {a["type"] for a in summary["anomalies"]}
    assert any(v[0].startswith("I2") for v in summary["invariant_violations"])     # a free ride
    [end] = [r for r in records_of(summary) if r["request_id"] == next(
        s["request_id"] for s in summary["unexpected"] if s["why"] == "end 15 with timeout_not_charged")]
    payment = [d for d in end["decisions"] if d["step"] == "payment" and d["outcome"] in ("ok", "failed")][-1]
    assert payment["outcome"] == "failed" and payment["detail"]["gateway_outcome"] == "timeout_not_charged"


def test_a_validation_bug_end_accepts_an_out_of_range_latitude(tmp_path, scenarios_dir):
    copy = planted_copy(tmp_path, "app.py", "\tif not -90 <= end_lat <= 90:", "\tif not -90 <= end_lat <= 900:")
    code, summary = run_scenario("errors", copy)
    assert code == 1
    [bad] = [s for s in summary["unexpected"] if s["why"] == "end with lat out of range"]
    assert "expected 422 invalid_coordinate, got 500 internal_error" == bad["problem"]
    [end] = [r for r in records_of(summary) if r["request_id"] == bad["request_id"]]
    steps = [(d["step"], d["outcome"]) for d in end["decisions"]]
    # the record says validation let lat=91 through ("params: ok"), and what then crashed
    assert ("params", "ok") in steps and end["params"]["lat"] == "91"
    assert end["exception"] == {"type": "ValueError", "message": "Latitude must be in the [-90; 90] range."}  # geopy


def test_stale_bytecode_cant_hide_a_planted_bug(tmp_path, scenarios_dir):
    # compile the clean code into __pycache__, then plant the bug with the same size and mtime:
    # a normal import would run the stale bytecode; the scenario's private cache doesn't
    old, new = "\tif not -90 <= end_lat <= 90:", "\tif not -90 <= end_lat <= 99:"   # same length
    copy = planted_copy(tmp_path, "app.py", old, old)          # an unchanged copy first
    subprocess.run([sys.executable, "-c", "import app"], cwd=copy, check=True, capture_output=True,
                   env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")})
    app_py = copy / "app.py"
    stat = app_py.stat()
    app_py.write_text(app_py.read_text().replace(old, new))
    os.utime(app_py, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert app_py.stat().st_size == stat.st_size and list((copy / "__pycache__").glob("app.*.pyc"))
    code, summary = run_scenario("errors", copy)
    assert code == 1 and any(s["why"] == "end with lat out of range" for s in summary["unexpected"])


# ---- the playbook's failing-test template is real code ---------------------------------------------

def _template():
    text = (REPO / "AGENTS.md").read_text()
    start = text.index("```python\ndef test_ending_a_ride_frees_the_scooter")
    return text[start + len("```python\n"):text.index("```", start + 3)]


def test_the_playbook_template_passes_on_the_real_code(client, read_db, read_records):
    # the fresh-clone walkthrough caught the first version of this template failing on
    # correct code (it took the LAST record, which was the /view_all_available check)
    namespace = {}
    exec(_template(), namespace)
    namespace["test_ending_a_ride_frees_the_scooter"](client, read_db, read_records)


# ---- the MCP tools read a scenario run ------------------------------------------------------------

def _call(name, args):
    async def go():
        async with Client(mcp_server.server) as client:
            return await client.call_tool(name, args)
    return anyio.run(go)


def test_mcp_tools_read_a_scenario_run(scenarios_dir):
    run_scenario("decline")
    res = _call("failure_counts", {"scenario": "decline"})
    assert not res.is_error and res.structured_content["result"] == {"payment_declined": 1}
    assert res.structured_content["files_read"]["records"]["path"] == str((scenarios_dir / "decline" / "records.jsonl").resolve())
    assert _call("scooter_state", {"scooter_id": "18", "scenario": "decline"}).structured_content["result"][0][
        "outstanding_balances"][0]["reason"] == "payment_declined"
    assert "run `make scenario NAME=rides`" in _call("failure_counts", {"scenario": "rides"}).content[0].text
    assert "lowercase letters" in _call("failure_counts", {"scenario": "../x"}).content[0].text


# ---- make reset-local: back up, then reset; deletes nothing ---------------------------------------

_spec = importlib.util.spec_from_file_location("reset_local", REPO / "scripts" / "reset_local.py")
reset_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset_local)


@pytest.fixture
def scratch_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    (root / "scooter_db.json").write_text('[{"id": "1"}]')
    (root / "data").mkdir()
    (root / "data" / ".gitkeep").write_text("")
    subprocess.run([*git, "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "seed"], cwd=root, check=True)
    (root / "scooter_db.json").write_text('[{"id": "1", "is_reserved": true}]')
    for name in ("records.jsonl", "payments_ledger.jsonl"):
        (root / "data" / name).write_text("{}\n")
    (root / "data" / "scenarios" / "rides").mkdir(parents=True)
    (root / "data" / "scenarios" / "rides" / "records.jsonl").write_text("{}\n")
    (root / "scooter_db.json.lock").write_text("")
    (root / ".scooter_db.json.abc123.tmp").write_text("partial")
    return root


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)


def test_reset_local_backs_everything_up_and_resets(scratch_repo, capsys):
    before = {p: (scratch_repo / p).read_bytes() for p in _files(scratch_repo)}
    assert reset_local.main(["--root", str(scratch_repo)]) == 0
    assert (scratch_repo / "scooter_db.json").read_text() == '[{"id": "1"}]'         # the committed file
    assert sorted(p.name for p in (scratch_repo / "data").iterdir()) == [".gitkeep"]
    assert not (scratch_repo / "scooter_db.json.lock").exists() and not list(scratch_repo.glob(".scooter_db*.tmp"))
    [backup] = (scratch_repo / "local-backups").iterdir()
    for path, content in before.items():                                              # nothing lost
        if path == "data/.gitkeep":
            continue
        assert (backup / path).read_bytes() == content, path
    assert "Undo: move the files back" in capsys.readouterr().out


def test_reset_local_recreates_a_missing_data_dir(scratch_repo):
    shutil.rmtree(scratch_repo / "data")
    assert reset_local.main(["--root", str(scratch_repo)]) == 0
    assert sorted(p.name for p in (scratch_repo / "data").iterdir()) == [".gitkeep"]


def test_reset_local_dry_run_changes_nothing(scratch_repo):
    before = {p: (scratch_repo / p).read_bytes() for p in _files(scratch_repo)}
    assert reset_local.main(["--root", str(scratch_repo), "--dry-run"]) == 0
    assert {p: (scratch_repo / p).read_bytes() for p in _files(scratch_repo)} == before


def test_reset_local_refuses_while_the_db_lock_is_held(scratch_repo):
    import fcntl
    before = _files(scratch_repo)
    with open(scratch_repo / "scooter_db.json.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert reset_local.main(["--root", str(scratch_repo)]) == 1
    assert _files(scratch_repo) == before


def test_two_resets_never_share_a_backup(scratch_repo, monkeypatch):
    monkeypatch.setattr(reset_local, "_stamp", lambda: "20261110T000000Z")     # the same second
    assert reset_local.main(["--root", str(scratch_repo)]) == 0
    (scratch_repo / "data" / "records.jsonl").write_text("second\n")
    assert reset_local.main(["--root", str(scratch_repo)]) == 0
    backups = sorted(p.name for p in (scratch_repo / "local-backups").iterdir())
    assert backups == ["20261110T000000Z", "20261110T000000Z-1"]
    assert (scratch_repo / "local-backups" / backups[1] / "data" / "records.jsonl").read_text() == "second\n"
