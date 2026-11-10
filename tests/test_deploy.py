"""Deploy readiness (PLAN PR 19): /health, the Procfile and app.json. Nothing is deployed."""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import app as app_module
import payments
from failures import FailureKind
from test_failure_kinds import assert_envelope

REPO = Path(__file__).resolve().parent.parent
HEROKU_ROUTER_TIMEOUT_S = 30    # devcenter.heroku.com/articles/request-timeout
HEROKU_SHUTDOWN_GRACE_S = 30    # SIGTERM, then SIGKILL 30 s later (dyno shutdown behaviour)


# ---- /health ---------------------------------------------------------------------------

def test_health_reports_a_readable_db(get_json, read_records):
    body = get_json("/health", 200)
    # seed: 5 scooters, 33 reserved (legacy, active)
    assert body == {"status": "ok", "db": {"readable": True, "scooters": 5, "reserved": 1, "payment_pending": 0,
                                           "unsupported": 0}}
    [record] = read_records()
    assert record["path"] == "/health" and record["status"] == 200 and record["error"] is None
    assert {"step": "health", "outcome": "ok",
            "detail": {"readable": True, "scooters": 5, "reserved": 1, "payment_pending": 0,
                       "unsupported": 0}} in record["decisions"]
    assert record["db_write"] == {"attempted": False, "ok": None, "error": None}


def test_health_counts_payment_pending_rides(client, get_json, monkeypatch):
    # A stuck ride is visible from outside: payment_pending stays > 0 until reconcile runs.
    monkeypatch.setenv("PAYMENT_FAULT", "timeout_not_charged")
    assert client.get("/reservation/start?id=18").status_code == 200
    assert client.get("/reservation/end?id=18&lat=23.01&lng=19.01").status_code == 504
    assert get_json("/health", 200) == {"status": "ok", "db": {"readable": True, "scooters": 5, "reserved": 2,
                                                               "payment_pending": 1, "unsupported": 0}}


def test_health_is_degraded_by_a_reservation_in_an_unknown_payment_state(client, get_json, db_path, read_records):
    # start/end refuse that scooter (storage_error), so "ok" would hide it (PR #26 review).
    # Still 200: the server works for every other scooter and a restart wouldn't fix the data.
    seed = json.loads(db_path.read_text())
    [entry] = [e for e in seed if e["id"] == "33"]
    entry["reservation"] = {"reservation_id": "a" * 32, "started_at": None, "start_request_id": None,
                            "payment_state": "refund_requested", "idempotency_key": "a" * 32}
    db_path.write_text(json.dumps(seed))
    assert client.get("/reservation/start?id=33").status_code == 500
    assert get_json("/health", 200) == {"status": "degraded", "db": {"readable": True, "scooters": 5, "reserved": 1,
                                                                     "payment_pending": 0, "unsupported": 1}}
    assert read_records()[-1]["decisions"][-1]["outcome"] == "degraded"


def test_health_never_writes_or_locks(client, db_path):
    before = db_path.read_bytes()
    lock = Path(app_module.lock_path_for(str(db_path)))
    for _ in range(3):
        assert client.get("/health").status_code == 200
    assert db_path.read_bytes() == before and not lock.exists()


@pytest.mark.parametrize("content", ["{not json", '[{"id": "1"}]', ""])
def test_health_reports_a_corrupt_db_without_its_path(client, db_path, content):
    db_path.write_text(content)
    body = assert_envelope(client.get("/health"), FailureKind.STORAGE_ERROR)
    assert body["msg"].startswith("Error 500 - The scooter database is corrupt")
    assert str(db_path) not in body["msg"] and db_path.name not in body["msg"]


def test_health_reports_a_missing_db_without_its_path(client, monkeypatch, tmp_path):
    missing = tmp_path / "secret-dir" / "db.json"
    monkeypatch.setenv("SCOOTER_DB_PATH", str(missing))
    body = assert_envelope(client.get("/health"), FailureKind.STORAGE_ERROR)
    assert body["msg"] == "Error 500 - The scooter database could not be read (FileNotFoundError)."
    assert "secret-dir" not in json.dumps(body)


def test_health_is_get_only(client):
    assert_envelope(client.post("/health"), FailureKind.METHOD_NOT_ALLOWED)


# ---- Procfile --------------------------------------------------------------------------

def _web_command():
    lines = [line for line in (REPO / "Procfile").read_text().splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].startswith("web: "), lines
    return lines[0][len("web: "):]


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_procfile_binds_every_interface_on_port_and_has_no_control_socket():
    argv = shlex.split(_web_command())
    # gunicorn reads WEB_CONCURRENCY itself, at config load (int(os.environ[...])), so an empty
    # value crashes it whatever --workers says. The env prefix replaces an empty or unset
    # value with 2 before gunicorn starts (PR #26 review; AI_NOTES #46).
    assert argv[0] == "WEB_CONCURRENCY=${WEB_CONCURRENCY:-2}" and argv[1] == "gunicorn"
    assert argv[-1] == "app:app" and "--workers" not in argv
    assert _flag(argv, "--bind") == "0.0.0.0:$PORT"      # explicit: never gunicorn's 127.0.0.1:8000
    assert "--no-control-socket" in argv                 # gunicorn 26 would write ~/.gunicorn/


def test_procfile_timeouts_fit_the_lock_the_gateway_and_heroku():
    argv = shlex.split(_web_command())
    timeout, graceful = int(_flag(argv, "--timeout")), int(_flag(argv, "--graceful-timeout"))
    slowest_request_s = app_module.DEFAULT_DB_LOCK_TIMEOUT + payments.DEFAULT_TIMEOUT_DELAY_MS / 1000
    # Above the lock wait plus the gateway call, so a slow request gets our JSON (storage_busy),
    # not a killed worker's HTML 500; below the router's 30 s, so a hung worker is recycled.
    assert slowest_request_s * 2 < timeout < HEROKU_ROUTER_TIMEOUT_S
    # A worker finishing its request on SIGTERM must be done before Heroku's SIGKILL.
    assert timeout <= graceful < HEROKU_SHUTDOWN_GRACE_S
    assert float(json.loads((REPO / "app.json").read_text())["env"]["SCOOTER_DB_LOCK_TIMEOUT"]["value"]) \
        == app_module.DEFAULT_DB_LOCK_TIMEOUT


@pytest.mark.parametrize("web_concurrency,workers", [(None, 2), ("", 2), ("3", 3)],
                         ids=["unset", "empty", "three"])
def test_gunicorn_accepts_the_procfile_command(web_concurrency, workers):
    # The Procfile line runs through a shell on Heroku ($PORT, ${WEB_CONCURRENCY:-2}); run it
    # the same way with --print-config, which parses everything and starts nothing. "empty"
    # is the case that crashed gunicorn 26 (int('')) under the first Procfile.
    env = {k: v for k, v in os.environ.items() if k != "WEB_CONCURRENCY"}
    env["PORT"] = "8193"
    if web_concurrency is not None:
        env["WEB_CONCURRENCY"] = web_concurrency
    command = _web_command().replace(" gunicorn ", " " + shlex.quote(str(Path(sys.executable).parent / "gunicorn")) + " ", 1)
    out = subprocess.run(["bash", "-c", command + " --print-config"], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    config = dict(re.findall(r"^(\w+)\s+= (.*)$", out.stdout, re.M))
    assert config["bind"] == "['0.0.0.0:8193']"
    assert config["workers"] == str(workers)
    assert config["control_socket_disable"] == "True"
    assert (config["timeout"], config["graceful_timeout"]) == ("20", "25")


# ---- app.json --------------------------------------------------------------------------

def test_app_json_is_safe_for_production():
    meta = json.loads((REPO / "app.json").read_text())
    assert len(meta["name"]) <= 30 and meta["description"]
    assert meta["buildpacks"] == [{"url": "heroku/python"}]
    # One web dyno: the JSON DB, records and ledger live on that dyno's own filesystem (H5).
    assert meta["formation"]["web"]["quantity"] == 1 and set(meta["formation"]) == {"web"}
    env = meta["env"]
    assert "ENABLE_FAULT_INJECTION" not in env               # never on a deployed server
    assert env["PAYMENT_FAULT"]["value"] == "none"
    assert all(isinstance(v["value"], str) and v["value"] for v in env.values())   # app.json wants strings
    assert int(env["WEB_CONCURRENCY"]["value"]) >= 1


def test_python_version_is_a_major_version_heroku_accepts():
    # Heroku: .python-version with the major version only ("3.15", recommended) or an exact patch.
    assert (REPO / ".python-version").read_text().strip() == f"{sys.version_info.major}.{sys.version_info.minor}"


# ---- doubled slashes -------------------------------------------------------------------

@pytest.mark.parametrize("url", ["/reservation//start?id=18", "/reservation/end//?id=18&lat=0&lng=0",
                                 "/view_all_available//", "/reservation///end?id=33&lat=1&lng=1"])
def test_a_doubled_slash_is_a_json_404_not_an_html_redirect(client, read_db, url):
    # Werkzeug >= 1.0 answered /reservation//start with an HTML 308 to /reservation/start
    # (PR #9 review). The 2019 Werkzeug 0.14 had no slash merging: a 404. Now a JSON 404 again,
    # and nothing happens to the scooter.
    before = read_db()
    assert_envelope(client.get(url), FailureKind.ROUTE_NOT_FOUND)
    assert read_db() == before


def test_merge_slashes_is_off():
    assert app_module.app.url_map.merge_slashes is False
