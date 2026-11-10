"""records_query: pure query functions and the CLI (PLAN PR 10)."""
import datetime
import inspect
import json
import os
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

import app as app_module
import records_query as rq

REPO = Path(__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 11, 10, 12, 0, 0, tzinfo=datetime.UTC)


def ts(minutes_ago, ms=0):
    t = NOW - datetime.timedelta(minutes=minutes_ago) + datetime.timedelta(milliseconds=ms)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rec(rid, minutes_ago, path="/search", status=200, kind=None, scooter=None, **extra):
    record = {"ts": ts(minutes_ago), "request_id": rid, "method": "GET", "path": path,
              "status": status, "error": {"kind": kind, "msg": kind} if kind else None,
              "scooter_id": scooter, "db_write": {"attempted": False, "ok": None, "error": None},
              "scooter_before": None, "scooter_after": None, "scooter_in_memory": None}
    record.update(extra)
    return record


FREE = {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": False}
TAKEN = dict(FREE, is_reserved=True)
WROTE = {"attempted": True, "ok": True, "error": None}

FIXTURE = [
    # written out of ts order on purpose (several workers finish out of order)
    rec("r3", 30, path="/reservation/end", scooter="18", db_write=WROTE,
        scooter_before=TAKEN, scooter_after=FREE, scooter_in_memory=FREE),
    rec("r1", 120, kind="invalid_number", status=422),
    rec("r2", 90, path="/reservation/start", scooter="18", db_write=WROTE,
        scooter_before=FREE, scooter_after=TAKEN, scooter_in_memory=TAKEN),
    rec("r4", 59, kind="route_not_found", status=404, path="/nope"),
    rec("r5", 60, kind="invalid_number", status=422),            # exactly at the 1h boundary
    rec("r6", 61, kind="scooter_not_found", status=422, path="/reservation/start", scooter="99"),
    rec("r7", 5, kind="invalid_number", status=422),
]


@pytest.fixture
def fixture_file(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in FIXTURE))
    return path


# ---- time windows -------------------------------------------------------------------

@pytest.mark.parametrize("value,delta", [
    ("30s", datetime.timedelta(seconds=30)), ("15m", datetime.timedelta(minutes=15)),
    ("1h", datetime.timedelta(hours=1)), ("2d", datetime.timedelta(days=2)),
    ("1h30m", datetime.timedelta(minutes=90)), ("1d2h3m4s", datetime.timedelta(days=1, hours=2, minutes=3, seconds=4)),
    ("0s", datetime.timedelta(0)), (" 1h ", datetime.timedelta(hours=1)),
])
def test_parse_since_durations(value, delta):
    assert rq.parse_since(value, NOW) == NOW - delta


@pytest.mark.parametrize("value,expected", [
    ("2026-11-10T11:00:00Z", datetime.datetime(2026, 11, 10, 11, 0, tzinfo=datetime.UTC)),
    ("2026-11-10T11:00:00.500Z", datetime.datetime(2026, 11, 10, 11, 0, 0, 500000, tzinfo=datetime.UTC)),
    ("2026-11-10T13:00:00+02:00", datetime.datetime(2026, 11, 10, 11, 0, tzinfo=datetime.UTC)),
    ("2026-11-10T11:00:00", datetime.datetime(2026, 11, 10, 11, 0, tzinfo=datetime.UTC)),   # naive = UTC
    ("2026-11-10", datetime.datetime(2026, 11, 10, 0, 0, tzinfo=datetime.UTC)),
])
def test_parse_since_iso(value, expected):
    assert rq.parse_since(value, NOW) == expected


@pytest.mark.parametrize("value", ["", "h", "1x", "-1h", "1.5h", "yesterday", "2026-13-01"])
def test_parse_since_rejects_garbage(value):
    with pytest.raises(ValueError):
        rq.parse_since(value, NOW)


def test_window_is_inclusive_at_the_boundary():
    since = rq.parse_since("1h", NOW)
    assert rq.in_window(rec("a", 60), since)
    assert not rq.in_window({"ts": ts(60, ms=-1)}, since)
    assert rq.in_window({"ts": ts(60, ms=1)}, since)


def test_records_without_a_valid_ts_are_outside_every_window():
    since = rq.parse_since("1h", NOW)
    assert not rq.in_window({"error": {"kind": "x"}}, since)
    assert not rq.in_window({"ts": "not a time"}, since)
    assert rq.in_window({"ts": "not a time"}, None)


# ---- queries --------------------------------------------------------------------------

def test_failure_counts_all_time_and_last_hour(fixture_file):
    records = list(rq.read_records(fixture_file))
    assert rq.failure_counts(records) == {"invalid_number": 3, "route_not_found": 1, "scooter_not_found": 1}
    last_hour = rq.failure_counts(records, rq.parse_since("1h", NOW))
    assert last_hour == {"invalid_number": 2, "route_not_found": 1}      # r5 (exactly 1h) in, r6 out


def test_failure_counts_ignore_successes_and_order_by_count_then_kind():
    records = [rec("a", 1, kind="b_kind"), rec("b", 1, kind="a_kind"), rec("c", 1), rec("d", 1, kind="b_kind")]
    assert list(rq.failure_counts(records).items()) == [("b_kind", 2), ("a_kind", 1)]


def test_recent_failures_newest_first_with_limit_and_window(fixture_file):
    records = list(rq.read_records(fixture_file))
    assert [r["request_id"] for r in rq.recent_failures(records, limit=3)] == ["r7", "r4", "r5"]
    assert [r["request_id"] for r in rq.recent_failures(records, since=rq.parse_since("1h", NOW))] == ["r7", "r4", "r5"]
    assert [r["request_id"] for r in rq.recent_failures(records, limit=100)] == ["r7", "r4", "r5", "r6", "r1"]


def test_scooter_history_is_sorted_by_ts_not_file_order(fixture_file):
    history = rq.scooter_history(rq.read_records(fixture_file), "18")
    assert [r["request_id"] for r in history] == ["r2", "r3"]
    assert rq.scooter_history(rq.read_records(fixture_file), "nope") == []


def test_find_requests_returns_every_match():
    records = [rec("dup", 3), rec("other", 2), rec("dup", 1)]
    assert [r["ts"] for r in rq.find_requests(records, "dup")] == [ts(3), ts(1)]
    assert rq.find_requests(records, "missing") == []


def test_anomaly_write_not_persisted():
    bad = rec("x", 1, path="/reservation/end", scooter="18", db_write=WROTE,
              scooter_before=TAKEN, scooter_after=TAKEN, scooter_in_memory=FREE)
    found = rq.anomalies([bad])
    assert {a["type"] for a in found} == {"write_not_persisted", "end_left_reserved"}
    assert found[0]["detail"] in ({"scooter_after": TAKEN, "scooter_in_memory": FREE}, {"scooter_after": TAKEN})


def test_anomaly_success_without_write():
    found = rq.anomalies([rec("x", 1, path="/reservation/start", scooter="18", scooter_before=FREE)])
    assert [a["type"] for a in found] == ["success_without_write"]


def test_anomaly_end_left_reserved_only():
    record = rec("x", 1, path="/reservation/end", scooter="18", db_write=WROTE,
                 scooter_before=TAKEN, scooter_after=TAKEN, scooter_in_memory=TAKEN)
    assert [a["type"] for a in rq.anomalies([record])] == ["end_left_reserved"]


@pytest.mark.parametrize("record", [
    rec("ok", 1, path="/reservation/end", scooter="18", db_write=WROTE,
        scooter_before=TAKEN, scooter_after=FREE, scooter_in_memory=FREE),
    rec("refused", 1, path="/reservation/start", status=422, kind="scooter_already_reserved",
        scooter="18", scooter_before=TAKEN, scooter_after=TAKEN, scooter_in_memory=TAKEN),
    rec("failed_write", 1, path="/reservation/start", status=500, kind="storage_error", scooter="18",
        db_write={"attempted": True, "ok": False, "error": "PermissionError"}, scooter_before=FREE),
    rec("search", 1),
    rec("start_ok", 1, path="/reservation/start", scooter="18", db_write=WROTE,
        scooter_before=FREE, scooter_after=TAKEN, scooter_in_memory=TAKEN),
])
def test_consistent_records_are_not_anomalies(record):
    assert rq.anomalies([record]) == []


def test_anomalies_respect_the_window():
    old = rec("x", 120, path="/reservation/start", scooter="18")
    assert rq.anomalies([old], since=rq.parse_since("1h", NOW)) == []
    assert len(rq.anomalies([old])) == 1


def test_query_functions_accept_any_iterable():
    # The MCP server (PLAN PR 16) will pass records from anywhere, not just a file.
    assert rq.failure_counts(iter(FIXTURE)) == rq.failure_counts(FIXTURE)
    assert inspect.isgenerator(rq.read_records(os.devnull))


# ---- reading: malformed lines, streaming ----------------------------------------------

def test_malformed_lines_are_skipped_with_warnings(tmp_path):
    path = tmp_path / "r.jsonl"
    good = json.dumps(rec("good", 1, kind="invalid_number"))
    path.write_text(f"{good}\nnot json\n\n42\n[1, 2]\n{good}\n" + '{"request_id": "half", "ts": "2026')
    warnings = []
    records = list(rq.read_records(path, warn=lambda n, reason: warnings.append((n, reason))))
    assert [r["request_id"] for r in records] == ["good", "good"]
    assert [n for n, _ in warnings] == [2, 4, 5, 7]
    assert "not valid JSON" in warnings[0][1]
    assert "not a JSON object (int)" in warnings[1][1] and "(list)" in warnings[2][1]
    assert "partially written" in warnings[3][1]


def test_only_newline_splits_records(tmp_path):
    path = tmp_path / "r.jsonl"
    odd = rec("odd", 1, scooter_id_note="a b\u0085c\rd")
    path.write_text(json.dumps(odd, ensure_ascii=False) + "\n", encoding="utf-8")
    assert [r["scooter_id_note"] for r in rq.read_records(path)] == ["a b\u0085c\rd"]


def test_large_files_are_streamed(tmp_path):
    path = tmp_path / "big.jsonl"
    line = json.dumps(rec("x", 1, kind="invalid_number", pad="p" * 400)) + "\n"
    with open(path, "w") as handle:
        for _ in range(40_000):
            handle.write(line)
    size = path.stat().st_size
    assert size > 15_000_000
    tracemalloc.start()
    try:
        counts = rq.failure_counts(rq.read_records(path))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert counts == {"invalid_number": 40_000}
    assert peak < size / 10, f"peak {peak} bytes for a {size}-byte file: not streaming"


# ---- CLI --------------------------------------------------------------------------------

def run(argv, capsys):
    code = rq.main(argv, now=NOW)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_failure_counts_human_and_json(fixture_file, capsys):
    code, out, _ = run(["--file", str(fixture_file), "failure-counts", "--since", "1h"], capsys)
    assert code == 0 and "3 total" in out and "invalid_number" in out and "scooter_not_found" not in out
    code, out, _ = run(["--file", str(fixture_file), "--json", "failure-counts", "--since", "1h"], capsys)
    assert code == 0 and json.loads(out) == {"invalid_number": 2, "route_not_found": 1}


def test_cli_recent_failures(fixture_file, capsys):
    code, out, _ = run(["--file", str(fixture_file), "--json", "recent-failures", "--limit", "2"], capsys)
    assert code == 0 and [r["request_id"] for r in json.loads(out)] == ["r7", "r4"]
    code, out, _ = run(["--file", str(fixture_file), "recent-failures"], capsys)
    assert code == 0 and out.splitlines()[0].startswith("5 most recent failure(s)")


def test_cli_scooter_and_request(fixture_file, capsys):
    code, out, _ = run(["--file", str(fixture_file), "--json", "scooter", "18"], capsys)
    assert code == 0 and [r["request_id"] for r in json.loads(out)] == ["r2", "r3"]
    code, out, _ = run(["--file", str(fixture_file), "scooter", "18"], capsys)
    assert "before=free@(23.0,19.0) after=reserved@(23.0,19.0)" in out
    code, out, _ = run(["--file", str(fixture_file), "--json", "request", "r4"], capsys)
    assert code == 0 and json.loads(out)[0]["path"] == "/nope"


@pytest.mark.parametrize("argv", [["scooter", "nope"], ["request", "nope"]])
def test_cli_not_found_exits_1(fixture_file, capsys, argv):
    code, out, err = run(["--file", str(fixture_file), *argv], capsys)
    assert code == 1 and "no records for" in err and "'nope'" in err


def test_cli_anomalies_exit_codes(tmp_path, capsys):
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(rec("ok", 1)) + "\n")
    code, out, _ = run(["--file", str(path), "anomalies"], capsys)
    assert (code, out.splitlines()[0]) == (0, "0 anomaly(ies)")
    with open(path, "a") as handle:
        handle.write(json.dumps(rec("bad", 1, path="/reservation/start", scooter="18")) + "\n")
    code, out, _ = run(["--file", str(path), "--json", "anomalies"], capsys)
    assert code == 1 and [a["request_id"] for a in json.loads(out)] == ["bad"]


def test_cli_missing_file_exits_2(tmp_path, capsys):
    code, _, err = run(["--file", str(tmp_path / "missing.jsonl"), "failure-counts"], capsys)
    assert code == 2 and "cannot read records file" in err


def test_cli_directory_as_file_exits_2(tmp_path, capsys):
    code, _, err = run(["--file", str(tmp_path), "failure-counts"], capsys)
    assert code == 2 and "cannot read records file" in err


@pytest.mark.parametrize("argv", [
    ["failure-counts", "--since", "bogus"], ["recent-failures", "--limit", "0"],
    ["recent-failures", "--limit", "x"], ["no-such-command"], [], ["scooter"],
])
def test_cli_bad_arguments_exit_2(fixture_file, capsys, argv):
    with pytest.raises(SystemExit) as exc:
        rq.main(["--file", str(fixture_file), *argv], now=NOW)
    assert exc.value.code == 2
    assert capsys.readouterr().err


def test_cli_warns_on_stderr_and_keeps_stdout_clean(tmp_path, capsys):
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(rec("a", 1, kind="invalid_number")) + "\n" + '{"half": ')
    code, out, err = run(["--file", str(path), "--json", "failure-counts"], capsys)
    assert code == 0 and json.loads(out) == {"invalid_number": 1}
    assert "skipped line" in err and ":2:" in err


def test_cli_uses_scooter_records_path_and_runs_as_a_module(fixture_file):
    env = dict(os.environ, SCOOTER_RECORDS_PATH=str(fixture_file))
    out = subprocess.run([sys.executable, "-m", "records_query", "--json", "request", "r1"],
                         cwd=REPO, env=env, capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout)[0]["request_id"] == "r1"


# ---- on records produced by real requests ------------------------------------------------

def test_cli_on_real_records(client, records_path, capsys):
    client.get("/reservation/start?id=18")
    client.get("/reservation/start?id=18")
    rid = client.get("/reservation/end?id=18&lat=23.01&lng=19.01").headers["X-Request-ID"]
    client.get("/nope")
    # NOW is a fixed test clock; these records carry the real time, so use the real clock:
    code, out, _ = run(["--file", str(records_path), "--json", "failure-counts"], capsys)
    assert code == 0 and json.loads(out) == {"route_not_found": 1, "scooter_already_reserved": 1}
    counts = rq.failure_counts(rq.read_records(records_path), rq.parse_since("1h"))
    assert counts == {"route_not_found": 1, "scooter_already_reserved": 1}
    history = rq.scooter_history(rq.read_records(records_path), "18")
    assert [(r["path"], r["status"]) for r in history] == [
        ("/reservation/start", 200), ("/reservation/start", 422), ("/reservation/end", 200)]
    assert rq.find_requests(rq.read_records(records_path), rid)[0]["scooter_after"]["is_reserved"] is False
    assert rq.anomalies(rq.read_records(records_path)) == []


def test_anomalies_catch_a_lost_write_in_real_records(client, monkeypatch, records_path):
    real = app_module.convert_db_to_dictlist

    def stale(db):                       # the end's change never reaches the file
        rows = real(db)
        for row in rows:
            if row["id"] == "18":
                row.update(lat=23.0, lng=19.0, is_reserved=True)
        return rows
    client.get("/reservation/start?id=18")
    monkeypatch.setattr(app_module, "convert_db_to_dictlist", stale)
    client.get("/reservation/end?id=18&lat=23.01&lng=19.01")
    found = rq.anomalies(rq.read_records(records_path))
    assert {a["type"] for a in found} == {"write_not_persisted", "end_left_reserved", "end_location_not_persisted"}
    assert {a["path"] for a in found} == {"/reservation/end"}


def test_anomalies_catch_a_success_without_write_in_real_records(client, monkeypatch, records_path):
    monkeypatch.setattr(app_module, "write_db", lambda db: True)      # a "forgot to save" bug
    assert client.get("/reservation/start?id=18").status_code == 200
    assert [a["type"] for a in rq.anomalies(rq.read_records(records_path))] == ["success_without_write"]


# ---- PR #12 review: overflow, BOM, start-side and location anomalies -------------------

@pytest.mark.parametrize("value", ["99999999d", "1000000000000h", "9" * 30 + "s"])
def test_huge_durations_are_a_clean_bad_argument(value, fixture_file, capsys):
    with pytest.raises(ValueError, match="reaches back further"):
        rq.parse_since(value, NOW)
    with pytest.raises(SystemExit) as exc:
        rq.main(["--file", str(fixture_file), "failure-counts", "--since", value], now=NOW)
    assert exc.value.code == 2 and "reaches back further" in capsys.readouterr().err


def test_a_byte_order_mark_does_not_swallow_the_first_record(tmp_path):
    path = tmp_path / "bom.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + (json.dumps(rec("first", 1)) + "\n" + json.dumps(rec("second", 1)) + "\n").encode())
    warnings = []
    assert [r["request_id"] for r in rq.read_records(path, warn=lambda *a: warnings.append(a))] == ["first", "second"]
    assert warnings == []


def _end(after, lat="23.01", lng="19.01", **extra):
    return rec("x", 1, path="/reservation/end", scooter="18", db_write=WROTE, params={"id": "18", "lat": lat, "lng": lng},
               scooter_before=TAKEN, scooter_after=after, scooter_in_memory=after, **extra)


def test_anomaly_start_left_free():
    record = rec("x", 1, path="/reservation/start", scooter="18", db_write=WROTE,
                 scooter_before=FREE, scooter_after=FREE, scooter_in_memory=FREE)
    assert [a["type"] for a in rq.anomalies([record])] == ["start_left_free"]


def test_anomaly_end_location_not_persisted():
    moved_wrong = dict(FREE)                                # freed, but still at the old spot
    found = rq.anomalies([_end(moved_wrong)])
    assert [a["type"] for a in found] == ["end_location_not_persisted"]
    assert found[0]["detail"]["requested"] == {"lat": 23.01, "lng": 19.01}


@pytest.mark.parametrize("lat,lng", [("23.01", "19.01"), ("+23.01", " 19.010 "), (["23.01", "1"], "19.01")])
def test_end_location_compares_as_floats_like_the_app(lat, lng):
    after = {"id": "18", "lat": 23.01, "lng": 19.01, "is_reserved": False}
    assert rq.anomalies([_end(after, lat=lat, lng=lng)]) == []


def test_end_location_rule_skips_unparseable_params():
    assert rq.anomalies([_end(dict(FREE), lat="x")]) == []


def test_no_false_positives_on_every_legitimate_flow(client, monkeypatch, records_path):
    calls = [
        "/reservation/start?id=18",                       # start
        "/reservation/end?id=18&lat=23.01&lng=19.01",     # end
        "/reservation/start?id=18",                       # re-reserve after end
        "/reservation/start?id=18",                       # refused: already reserved
        "/reservation/start?id=33",                       # refused: seed-reserved
        "/reservation/end?id=15&lat=0&lng=0",             # refused: not reserved
        "/reservation/start?id=nope",                     # unknown scooter
        "/reservation/end?id=18&lat=+1e1&lng=٥",          # lenient float() coordinates
        "/reservation/start?id=18",
        "/search?lat=0&lng=0&radius=10", "/view_all_available", "/nope",
    ]
    for url in calls:
        client.get(url)
    monkeypatch.setattr(app_module, "payment_gateway", lambda cost: {"status": False})
    client.get("/reservation/end?id=18&lat=1&lng=1")      # failed payment: ride left open
    records = list(rq.read_records(records_path))
    assert len(records) == len(calls) + 1
    assert [r["status"] for r in records] == [200, 200, 200, 422, 422, 422, 422, 200, 200, 200, 200, 404, 502]
    assert rq.anomalies(records) == []


def _stale_convert(monkeypatch, **forced):
    real = app_module.convert_db_to_dictlist

    def stale(db):
        rows = real(db)
        for row in rows:
            if row["id"] == "18":
                row.update(forced)
        return rows
    monkeypatch.setattr(app_module, "convert_db_to_dictlist", stale)


def test_real_planted_start_bug_is_flagged(client, monkeypatch, records_path):
    _stale_convert(monkeypatch, is_reserved=False)        # start "succeeds" but writes it free
    assert client.get("/reservation/start?id=18").status_code == 200
    found = rq.anomalies(rq.read_records(records_path))
    assert {a["type"] for a in found} == {"start_left_free", "write_not_persisted"}


def test_real_planted_end_location_bug_is_flagged(client, monkeypatch, records_path):
    client.get("/reservation/start?id=18")
    _stale_convert(monkeypatch, lat=23.0, lng=19.0)        # freed, but the move isn't written
    assert client.get("/reservation/end?id=18&lat=23.01&lng=19.01").status_code == 200
    found = rq.anomalies(rq.read_records(records_path))
    assert {a["type"] for a in found} == {"end_location_not_persisted", "write_not_persisted"}


_LOCATION_LINE = "\t\t\t\tscooter.lat, scooter.lng = end_lat, end_lng\n"


def test_real_planted_bug_that_never_moves_the_scooter_is_flagged(tmp_path, db_path, records_path):
    # A bug the in-memory comparison can't see: the code itself forgets to move the scooter,
    # so scooter_after == scooter_in_memory. Only the requested-vs-persisted rule flags it.
    src = (REPO / "app.py").read_text()
    assert src.count(_LOCATION_LINE) == 1, "end_reservation changed shape; update this test"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "app.py").write_text(src.replace(_LOCATION_LINE, ""))
    for name in ("records.py", "failures.py"):
        (planted / name).write_text((REPO / name).read_text())
    script = ("import sys; sys.path.insert(0, sys.argv[1]); import app; c = app.app.test_client(); "
              "c.get('/reservation/start?id=18'); print(c.get('/reservation/end?id=18&lat=23.01&lng=19.01').status_code)")
    env = dict(os.environ, SCOOTER_DB_PATH=str(db_path), SCOOTER_RECORDS_PATH=str(records_path), PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run([sys.executable, "-c", script, str(planted)], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "200"
    found = rq.anomalies(rq.read_records(records_path))
    assert [a["type"] for a in found] == ["end_location_not_persisted"]


def test_start_rule_only_applies_to_successful_starts():
    # A failed start that leaves the scooter free (e.g. refused before writing) is fine.
    failed = rec("x", 1, path="/reservation/start", scooter="18", status=500, kind="internal_error",
                 scooter_before=FREE, scooter_after=FREE, scooter_in_memory=FREE)
    assert rq.anomalies([failed]) == []
