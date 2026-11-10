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


def test_anomalies_catch_a_lost_write_in_real_records(client, monkeypatch, records_path, read_db):
    real = app_module.convert_db_to_dictlist

    def stale(db):                       # the end's change never reaches the file
        rows = real(db)
        for row in rows:
            if row["id"] == "18":
                row.update(lat=23.0, lng=19.0, is_reserved=True, reservation=reservation)
        return rows
    client.get("/reservation/start?id=18")
    reservation = read_db()["18"]["reservation"]
    monkeypatch.setattr(app_module, "convert_db_to_dictlist", stale)
    client.get("/reservation/end?id=18&lat=23.01&lng=19.01")
    found = rq.anomalies(rq.read_records(records_path))
    assert {a["type"] for a in found} == {"write_not_persisted", "end_left_reserved", "end_location_not_persisted"}
    assert {a["path"] for a in found} == {"/reservation/end"}


def test_anomalies_catch_a_success_without_write_in_real_records(client, monkeypatch, records_path):
    monkeypatch.setattr(app_module, "write_db", lambda db, path=None: True)   # a "forgot to save" bug
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


def test_no_false_positives_on_every_legitimate_flow(client, monkeypatch, records_path, db_path):
    calls = [
        "/reservation/start?id=18",                       # start
        "/reservation/end?id=18&lat=23.01&lng=19.01",     # end
        "/reservation/start?id=18",                       # re-reserve after end
        "/reservation/start?id=18",                       # refused: already reserved
        "/reservation/start?id=33",                       # refused: seed-reserved (legacy reservation)
        "/reservation/end?id=33&lat=24&lng=-28",          # end of a legacy reservation
        "/reservation/start?id=33",                       # re-reserve after a legacy end
        "/reservation/end?id=15&lat=0&lng=0",             # refused: not reserved
        "/reservation/start?id=nope",                     # unknown scooter
        "/reservation/end?id=18&lat=+1e1&lng=٥",          # lenient float() coordinates
        "/reservation/start?id=18",
        "/search?lat=0&lng=0&radius=10", "/view_all_available", "/nope",
    ]
    for url in calls:
        client.get(url)
    # every payment outcome and the payment_pending flows (PLAN PRs 13/14)
    payment_calls = [
        ("error", "/reservation/end?id=18&lat=1&lng=1", 502),                 # -> payment_pending
        ("none", "/reservation/start?id=18", 422),                            # refused: payment pending
        ("timeout_not_charged", "/reservation/end?id=18&lat=7&lng=7", 504),   # retry (stored location)
        ("timeout_charged", "/reservation/end?id=18&lat=1&lng=1", 504),       # retry, charged
        ("none", "/reservation/end?id=18&lat=2&lng=2", 200),                  # already_charged at (1, 1)
        ("none", "/reservation/start?id=18", 200),
        ("decline", "/reservation/end?id=18&lat=3&lng=3", 422),               # H3: ended, balance
        ("none", "/reservation/start?id=18", 200),
        ("none", "/reservation/end?id=18&lat=4&lng=4", 200),                  # plain success
    ]
    for fault, url, status in payment_calls:
        monkeypatch.setenv("PAYMENT_FAULT", fault)
        assert client.get(url).status_code == status, url
    # a restored DB ended elsewhere: conflict, back to active
    client.get("/reservation/start?id=18")
    snapshot = db_path.read_bytes()
    assert client.get("/reservation/end?id=18&lat=5&lng=5").status_code == 200
    db_path.write_bytes(snapshot)
    assert client.get("/reservation/end?id=18&lat=6&lng=6").status_code == 409
    records = list(rq.read_records(records_path))
    assert len(records) == len(calls) + len(payment_calls) + 3
    assert [r["status"] for r in records] == [200, 200, 200, 422, 422, 200, 200, 422, 422, 200, 200, 200, 200, 404,
                                              *[status for _, _, status in payment_calls], 200, 200, 409]
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
    _stale_convert(monkeypatch, is_reserved=False, reservation=None)   # start "succeeds" but writes it free
    assert client.get("/reservation/start?id=18").status_code == 200
    found = rq.anomalies(rq.read_records(records_path))
    assert {a["type"] for a in found} == {"start_left_free", "write_not_persisted", "start_reservation_not_persisted"}


def test_real_planted_end_location_bug_is_flagged(client, monkeypatch, records_path):
    client.get("/reservation/start?id=18")
    _stale_convert(monkeypatch, lat=23.0, lng=19.0)        # freed, but the move isn't written
    assert client.get("/reservation/end?id=18&lat=23.01&lng=19.01").status_code == 200
    found = rq.anomalies(rq.read_records(records_path))
    assert {a["type"] for a in found} == {"end_location_not_persisted", "write_not_persisted"}


_LOCATION_LINE = "\tscooter.lat, scooter.lng = end_lat, end_lng\n"     # in finish_ride


def test_real_planted_bug_that_never_moves_the_scooter_is_flagged(tmp_path, db_path, records_path):
    # A bug the in-memory comparison can't see: the code itself forgets to move the scooter,
    # so scooter_after == scooter_in_memory. Only the requested-vs-persisted rule flags it.
    src = (REPO / "app.py").read_text()
    assert src.count(_LOCATION_LINE) == 1, "end_reservation changed shape; update this test"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "app.py").write_text(src.replace(_LOCATION_LINE, ""))
    for name in ("records.py", "failures.py", "payments.py"):
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


# ---- reservation rules (PLAN PR 12) --------------------------------------------------------

RES = {"reservation_id": "a" * 32, "started_at": "2026-11-09T12:00:00.000Z", "start_request_id": "x",
       "payment_state": "active", "idempotency_key": "a" * 32}
FREE12 = dict(FREE, reservation=None)                     # the post-PR-12 snapshot shape
TAKEN12 = dict(TAKEN, reservation=RES)


@pytest.mark.parametrize("after", [dict(FREE, reservation=RES), dict(TAKEN, reservation=None)])
def test_anomaly_reservation_flag_mismatch(after):
    record = rec("other", 1, path="/reservation/end", status=422, kind="no_active_reservation", scooter="18",
                 scooter_before=after, scooter_after=after, scooter_in_memory=after)
    found = rq.anomalies([record])
    assert [a["type"] for a in found] == ["reservation_flag_mismatch"]
    assert found[0]["detail"] == {"scooter_after": after}


@pytest.mark.parametrize("reservation", [None, "x", dict(RES, start_request_id="someone-else"),
                                         dict(RES, start_request_id=None)])
def test_anomaly_start_reservation_not_persisted(reservation):
    after = dict(TAKEN, reservation=reservation)
    record = rec("x", 1, path="/reservation/start", scooter="18", db_write=WROTE,
                 scooter_before=FREE12, scooter_after=after, scooter_in_memory=after)
    types = [a["type"] for a in rq.anomalies([record])]
    assert "start_reservation_not_persisted" in types
    assert set(types) <= {"start_reservation_not_persisted", "reservation_flag_mismatch"}


@pytest.mark.parametrize("record", [
    rec("x", 1, path="/reservation/start", scooter="18", db_write=WROTE,
        scooter_before=FREE12, scooter_after=TAKEN12, scooter_in_memory=TAKEN12),
    rec("ok", 1, path="/reservation/end", scooter="18", db_write=WROTE,
        scooter_before=TAKEN12, scooter_after=FREE12, scooter_in_memory=FREE12),
    rec("refused", 1, path="/reservation/start", status=422, kind="scooter_already_reserved", scooter="18",
        scooter_before=TAKEN12, scooter_after=TAKEN12, scooter_in_memory=TAKEN12),
    # pre-PLAN PR 12 records have no "reservation" key and aren't judged by these rules
    rec("old_start", 1, path="/reservation/start", scooter="18", db_write=WROTE,
        scooter_before=FREE, scooter_after=TAKEN, scooter_in_memory=TAKEN),
])
def test_reservation_rules_have_no_false_positives(record):
    assert rq.anomalies([record]) == []


def test_scooter_history_shows_each_payment_outcome(client, monkeypatch, records_path, capsys):
    client.get("/reservation/start?id=18")
    monkeypatch.setenv("PAYMENT_FAULT", "error")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    code, out, _ = run(["--file", str(records_path), "scooter", "18"], capsys)
    assert code == 0
    lines = [line for line in out.splitlines() if "before=" in line]
    assert ["payment=" in line for line in lines] == [False, True, True]
    assert lines[1].endswith("payment=error") and lines[2].endswith("payment=success")
    old = {"decisions": [{"step": "payment", "outcome": "ok", "detail": {"txn_id": 1}}]}   # pre-PR 13 record
    assert rq._payment(old) == "ok" and rq._payment({"decisions": []}) is None


# ---- end-ride payment rules (PLAN PR 14) --------------------------------------------------------

PENDING12 = dict(TAKEN, reservation=dict(RES, payment_state="payment_pending", end_lat=1.0, end_lng=1.0, amount=5))
PAID = {"step": "payment", "outcome": "ok", "detail": {"gateway_outcome": "success", "txn_id": "t"}}


def _write(outcome):
    return {"step": "db_write", "outcome": outcome, "detail": {}}


@pytest.mark.parametrize("decisions,flagged", [
    ([PAID, _write("failed")], True),
    ([PAID, _write("refused")], True),
    ([_write("ok"), PAID, _write("failed")], True),                  # phase 1 ok, phase 2 lost
    ([PAID, _write("ok")], False),
    ([_write("failed")], False),                                      # failed before any charge
    ([{"step": "payment", "outcome": "failed", "detail": {}}, _write("failed")], False),
])
def test_anomaly_charged_without_persisted_end(decisions, flagged):
    record = rec("x", 1, path="/reservation/end", scooter="18", status=500, kind="storage_error",
                 decisions=decisions, scooter_before=TAKEN12)
    types = [a["type"] for a in rq.anomalies([record])]
    assert ("charged_without_persisted_end" in types) is flagged


BALANCE_FOR_RES = {"reservation_id": RES["reservation_id"], "amount": 5}


@pytest.mark.parametrize("after,flagged", [
    (dict(FREE12, outstanding_balances=[BALANCE_FOR_RES]), False),
    (FREE12, True),                                                    # ended, but no balance
    (dict(FREE12, outstanding_balances=[dict(BALANCE_FOR_RES, reservation_id="other")]), True),
    (dict(TAKEN12, outstanding_balances=[BALANCE_FOR_RES]), True),     # balance, but still reserved
])
def test_anomaly_decline_not_settled(after, flagged):
    record = rec("x", 1, path="/reservation/end", scooter="18", status=422, kind="payment_declined",
                 db_write=WROTE, scooter_before=TAKEN12, scooter_after=after, scooter_in_memory=after)
    types = [a["type"] for a in rq.anomalies([record])]
    assert ("decline_not_settled" in types) is flagged


@pytest.mark.parametrize("kind", ["payment_timeout", "payment_failed"])
@pytest.mark.parametrize("after,flagged", [(PENDING12, False), (TAKEN12, True), (FREE12, True), (None, False)])
def test_anomaly_failed_payment_not_pending(kind, after, flagged):
    record = rec("x", 1, path="/reservation/end", scooter="18", status=504, kind=kind,
                 db_write=WROTE, scooter_before=TAKEN12, scooter_after=after, scooter_in_memory=after)
    types = [a["type"] for a in rq.anomalies([record])]
    assert ("failed_payment_not_pending" in types) is flagged


# ---- PLAN PR 14b: rules that make the planted payment bugs visible ------------------------------

def _d(step, outcome, **detail):
    return {"step": step, "outcome": outcome, "detail": detail}


PENDING_D = _d("payment", "pending", end_lat=1.0, end_lng=1.0, distance_m=10, amount=10)
REQUESTED_D = _d("payment", "requested", idempotency_key="k", cost=10, distance_m=10, end_lat=1.0, end_lng=1.0)
OK_D = _d("payment", "ok", gateway_outcome="success", txn_id="t")
FAILED_D = _d("payment", "failed", gateway_outcome="error", txn_id=None)
WRITE_OK = _d("db_write", "ok")


def _end_rec(rid, decisions, status=200, kind=None, minutes_ago=1, **extra):
    return rec(rid, minutes_ago, path="/reservation/end", scooter="18", status=status, kind=kind,
               db_write=WROTE, decisions=decisions, **extra)


def _types(records, **kwargs):
    return sorted({a["type"] for a in rq.anomalies(records, **kwargs)})


@pytest.mark.parametrize("decisions,status,kind,flagged", [
    ([PENDING_D, WRITE_OK, REQUESTED_D, FAILED_D, WRITE_OK], 200, None, True),       # ended though the charge failed
    ([PENDING_D, WRITE_OK, REQUESTED_D, WRITE_OK], 200, None, True),                  # ended with no payment result
    ([PENDING_D, WRITE_OK, REQUESTED_D, OK_D, WRITE_OK], 200, None, False),
    ([PENDING_D, WRITE_OK, REQUESTED_D, _d("payment", "failed", gateway_outcome="decline"), WRITE_OK],
     422, "payment_declined", False),
    ([PENDING_D, WRITE_OK, REQUESTED_D, _d("payment", "failed", gateway_outcome="error"), WRITE_OK],
     422, "payment_declined", True),                                                    # "declined" without a decline
])
def test_anomaly_ended_without_payment(decisions, status, kind, flagged):
    assert ("ended_without_payment" in _types([_end_rec("x", decisions, status, kind)])) is flagged


@pytest.mark.parametrize("decisions,flagged", [
    ([PENDING_D, REQUESTED_D, OK_D, WRITE_OK], True),                                  # charged before phase 1 saved
    ([PENDING_D, _d("db_write", "failed"), REQUESTED_D, OK_D], True),
    ([PENDING_D, WRITE_OK, REQUESTED_D, OK_D, WRITE_OK], False),
    ([_d("end", "allowed"), REQUESTED_D, OK_D, WRITE_OK], False),                      # pre-PR-14 record: no pending
])
def test_anomaly_charged_before_pending_persisted(decisions, flagged):
    assert ("charged_before_pending_persisted" in _types([_end_rec("x", decisions)])) is flagged


RETRY_D = _d("end", "retry", end_lat=1.0, end_lng=1.0, amount=10, requested_lat=5.0, requested_lng=5.0)


def _retry_rec(rid, requested, retry=RETRY_D, extra_decisions=(), minutes_ago=1):
    return _end_rec(rid, [retry, *extra_decisions, requested, OK_D, WRITE_OK], minutes_ago=minutes_ago)


def test_anomaly_pending_location_changed():
    first = _end_rec("p", [PENDING_D, WRITE_OK, REQUESTED_D, FAILED_D, WRITE_OK], status=502, kind="payment_failed",
                     minutes_ago=2)
    moved = dict(REQUESTED_D, detail=dict(REQUESTED_D["detail"], end_lat=5.0, end_lng=5.0))
    # the retry re-priced inside its own request
    assert "pending_location_changed" in _types([first, _retry_rec("r", moved, extra_decisions=[PENDING_D, WRITE_OK])])
    # the retry charged somewhere other than its stored location (seen even without the
    # phase-1 record, e.g. when that record is in an older, rotated file)
    assert "pending_location_changed" in _types([first, _retry_rec("r", moved)])
    assert "pending_location_changed" in _types([_retry_rec("r", moved)])
    # stored and charged agree in the retry, but differ from what phase 1 fixed for the key
    shifted = _d("end", "retry", end_lat=5.0, end_lng=5.0, amount=10)
    assert "pending_location_changed" in _types([first, _retry_rec("r", moved, retry=shifted)])
    # a phase-1 record outside the --since window still supplies the stored values
    assert "pending_location_changed" in _types([dict(first, ts=ts(120)), _retry_rec("r", moved, retry=shifted)],
                                                since=NOW - datetime.timedelta(minutes=60))
    # legitimate retries: same values; or no phase-1 record known for the key
    assert _types([first, _retry_rec("r", REQUESTED_D)]) == []
    assert _types([_retry_rec("r", moved, retry=shifted)]) == []


def test_decline_not_settled_checks_the_balance_amount():
    declined = _d("payment", "failed", gateway_outcome="decline")
    before = TAKEN12
    for amount, flagged in ((10, False), (0, True), (11, True)):
        after = dict(FREE12, outstanding_balances=[{"reservation_id": RES["reservation_id"], "amount": amount}])
        record = _end_rec("x", [PENDING_D, WRITE_OK, REQUESTED_D, declined, WRITE_OK], status=422,
                          kind="payment_declined", scooter_before=before, scooter_after=after, scooter_in_memory=after)
        assert ("decline_not_settled" in _types([record])) is flagged, amount


# ---- payment-orphans: a ledger entry whose request left no record -----------------------------

def _ledger_entry(request_id, minutes_ago=1, charged=True):
    return {"ts": ts(minutes_ago), "idempotency_key": "k", "attempt": 1, "outcome": "success",
            "charged": charged, "request_id": request_id, "scooter_id": "18"}


def test_ledger_orphans():
    records = [rec("r1", 1)]
    entries = [_ledger_entry("r1"), _ledger_entry("dead"), _ledger_entry(None),
               _ledger_entry("young", minutes_ago=0)]
    found = rq.ledger_orphans(entries, records, now=NOW + datetime.timedelta(seconds=1), min_age_s=5)
    assert [e["request_id"] for e in found] == ["dead"]                # young may still be running


def test_cli_payment_orphans(tmp_path, capsys):
    records_file = tmp_path / "records.jsonl"
    records_file.write_text(json.dumps(rec("r1", 1)) + "\n")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(_ledger_entry("r1")) + "\n")
    assert rq.main(["--file", str(records_file), "payment-orphans", "--ledger", str(ledger)], now=NOW) == 0
    capsys.readouterr()
    with ledger.open("a") as f:
        f.write(json.dumps(_ledger_entry("killed-worker")) + "\n")
    assert rq.main(["--file", str(records_file), "payment-orphans", "--ledger", str(ledger)], now=NOW) == 1
    out = capsys.readouterr().out
    assert "1 ledger entr(ies) with no request record" in out and "request_id=killed-worker" in out
    assert rq.main(["--file", str(records_file), "--json", "payment-orphans", "--ledger", str(ledger)], now=NOW) == 1
    assert [e["request_id"] for e in json.loads(capsys.readouterr().out)] == ["killed-worker"]
    assert rq.main(["--file", str(records_file), "payment-orphans", "--ledger", str(tmp_path / "nope")], now=NOW) == 2


def test_payment_orphans_on_real_requests_is_clean(client, monkeypatch, records_path, ledger_path, capsys):
    client.get("/reservation/start?id=18")
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_charged")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    monkeypatch.setenv("PAYMENT_FAULT", "none")
    client.get("/reservation/end?id=18&lat=1&lng=1")
    assert rq.main(["--file", str(records_path), "payment-orphans", "--ledger", str(ledger_path), "--min-age", "0"]) == 0
