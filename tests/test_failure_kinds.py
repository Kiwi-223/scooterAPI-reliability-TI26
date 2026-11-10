"""Failure kinds: the registry is stable, and every kind is produced by a real request.

`test_every_kind_has_a_trigger` fails when a kind is added to failures.py without an entry
in TRIGGERS below, so no kind can ship untested.
"""
import json
import os

import flask
import pytest

import app as app_module
from failures import FailureKind
from messages import no_reservation, reserved_ok

# Ids are a public contract: once shipped, never renamed. Add new ids here; never edit or
# remove one. (Statuses can only change with a README change.)
SHIPPED = {
    "missing_param": 422,
    "invalid_number": 422,
    "invalid_coordinate": 422,
    "invalid_radius": 422,
    "scooter_not_found": 422,
    "scooter_already_reserved": 422,
    "no_active_reservation": 422,
    "payment_failed": 502,
    "route_not_found": 404,
    "method_not_allowed": 405,
    "storage_error": 500,
    "storage_busy": 503,
    "internal_error": 500,
}

def seed_18_reserved(reservation_id, on_disk):
    """Seed scooter 18, reserved by the start that returned reservation_id (checked against on_disk)."""
    reservation = on_disk["reservation"]
    assert isinstance(reservation, dict) and reservation["reservation_id"] == reservation_id
    return {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": True, "reservation": reservation}


def failing_gateway(calls):
    def gateway(cost):
        calls.append(cost)
        return {"status": False}
    return gateway


def _hold_lock_and_start(c, mp, tmp):
    # Another holder keeps the DB lock longer than SCOOTER_DB_LOCK_TIMEOUT.
    import fcntl
    mp.setenv("SCOOTER_DB_LOCK_TIMEOUT", "0.05")
    with open(app_module.lock_path_for(app_module.get_db_path()), "a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        return c.get("/reservation/start?id=18")


def _boom(distance):
    raise RuntimeError("planted bug")


# One real request per kind. Each takes (client, monkeypatch, tmp_path) and returns a response.
TRIGGERS = {
    FailureKind.MISSING_PARAM: lambda c, mp, tmp: c.get("/search?lat=0&lng=0"),
    FailureKind.INVALID_NUMBER: lambda c, mp, tmp: c.get("/search?lat=0&lng=0&radius=far"),
    FailureKind.INVALID_COORDINATE: lambda c, mp, tmp: c.get("/search?lat=91&lng=0&radius=1"),
    FailureKind.INVALID_RADIUS: lambda c, mp, tmp: c.get("/search?lat=0&lng=0&radius=-1"),
    FailureKind.SCOOTER_NOT_FOUND: lambda c, mp, tmp: c.get("/reservation/start?id=nope"),
    FailureKind.SCOOTER_ALREADY_RESERVED: lambda c, mp, tmp: c.get("/reservation/start?id=33"),
    FailureKind.NO_ACTIVE_RESERVATION: lambda c, mp, tmp: c.get("/reservation/end?id=18&lat=0&lng=0"),
    FailureKind.PAYMENT_FAILED: lambda c, mp, tmp: (
        mp.setattr(app_module, "payment_gateway", failing_gateway([])),
        c.get("/reservation/start?id=18"),
        c.get("/reservation/end?id=18&lat=1&lng=1"))[-1],
    FailureKind.ROUTE_NOT_FOUND: lambda c, mp, tmp: c.get("/no-such-route"),
    FailureKind.METHOD_NOT_ALLOWED: lambda c, mp, tmp: c.post("/search"),
    FailureKind.STORAGE_ERROR: lambda c, mp, tmp: (
        mp.setenv("SCOOTER_DB_PATH", str(tmp / "missing.json")),
        c.get("/view_all_available"))[-1],
    FailureKind.STORAGE_BUSY: _hold_lock_and_start,
    FailureKind.INTERNAL_ERROR: lambda c, mp, tmp: (
        mp.setattr(app_module, "calculate_cost", _boom),
        c.get("/reservation/start?id=18"),
        c.get("/reservation/end?id=18&lat=1&lng=1"))[-1],
}


def assert_envelope(resp, kind, status=None):
    body = resp.get_json()
    assert resp.status_code == (status or kind.status), resp.get_data(as_text=True)
    assert resp.mimetype == "application/json"
    assert set(body) == {"msg", "error"} and body["error"] == {"kind": kind.value}
    assert isinstance(body["msg"], str) and body["msg"]
    return body


# ---- the registry --------------------------------------------------------------------

def test_shipped_ids_are_never_renamed_or_removed():
    current = {kind.value: kind.status for kind in FailureKind}
    assert {k: current.get(k) for k in SHIPPED} == SHIPPED


def test_registry_has_no_unlisted_ids():
    # Adding a kind means adding it to SHIPPED (and to TRIGGERS, README and AGENTS.md).
    assert {kind.value for kind in FailureKind} == set(SHIPPED)


def test_every_kind_has_a_description_and_snake_case_id():
    for kind in FailureKind:
        assert kind.value.replace("_", "").isalpha() and kind.value == kind.value.lower()
        assert kind.description.strip()


def test_every_kind_has_a_trigger():
    assert set(TRIGGERS) == set(FailureKind)


@pytest.mark.parametrize("kind", list(FailureKind), ids=lambda k: k.value)
def test_every_kind_is_produced_by_a_real_request(kind, client, monkeypatch, tmp_path):
    assert_envelope(TRIGGERS[kind](client, monkeypatch, tmp_path), kind)


# ---- framework errors (404 / 405 / 500) ----------------------------------------------

def test_unknown_route_is_json_404(client):
    body = assert_envelope(client.get("/no-such-route"), FailureKind.ROUTE_NOT_FOUND)
    assert body["msg"] == "Error 404 - Not Found."


def test_wrong_method_is_json_405_and_keeps_allow_header(client):
    resp = client.post("/search")
    body = assert_envelope(resp, FailureKind.METHOD_NOT_ALLOWED)
    assert body["msg"] == "Error 405 - Method Not Allowed."
    assert "GET" in resp.headers["Allow"]


@pytest.mark.parametrize("testing", [True, False])
def test_unexpected_exception_is_json_500_in_both_modes(client, monkeypatch, read_db, caplog, testing):
    # The catch-all is registered for Exception, so it runs even with TESTING=True
    # (AGENTS.md "Flask 3 gotcha"). The traceback still reaches the log.
    monkeypatch.setitem(app_module.app.config, "TESTING", testing)
    monkeypatch.setattr(app_module, "calculate_cost", _boom)
    start = client.get("/reservation/start?id=18")
    assert start.status_code == 200
    reserved = read_db()["18"]
    body = assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.INTERNAL_ERROR)
    assert body["msg"] == "Error 500 - Internal server error."
    assert "planted bug" not in body["msg"]                 # no internals leak to clients
    assert "unhandled exception" in caplog.text and "RuntimeError: planted bug" in caplog.text
    assert read_db()["18"] == reserved == seed_18_reserved(start.get_json()["reservation_id"], reserved)  # the crash didn't write anything


@pytest.mark.parametrize("testing", [True, False])
def test_abort_500_is_json_internal_error(client, monkeypatch, testing):
    # abort(500) raises InternalServerError, which is itself an HTTPException: it must not
    # come back as Flask's HTML page (AI_NOTES #9).
    monkeypatch.setitem(app_module.app.config, "TESTING", testing)
    monkeypatch.setattr(app_module, "calculate_cost", lambda d: flask.abort(500))
    client.get("/reservation/start?id=18")
    body = assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.INTERNAL_ERROR)
    assert body["msg"] == "Error 500 - Internal Server Error."


def test_other_http_errors_keep_their_status(client, monkeypatch):
    # Not reachable through today's routes; any other HTTP error keeps its own status
    # and is reported as internal_error rather than as an HTML page.
    monkeypatch.setattr(app_module, "calculate_cost", lambda d: flask.abort(418))
    client.get("/reservation/start?id=18")
    assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.INTERNAL_ERROR, status=418)


# ---- storage --------------------------------------------------------------------------

def test_missing_db_is_storage_error(client, monkeypatch, tmp_path):
    monkeypatch.setenv("SCOOTER_DB_PATH", str(tmp_path / "missing.json"))
    body = assert_envelope(client.get("/view_all_available"), FailureKind.STORAGE_ERROR)
    assert body["msg"] == "Error 500 - The scooter database could not be read (FileNotFoundError)."


@pytest.mark.parametrize("content", ["{not json", '[{"id": "1"}]', "42"])
@pytest.mark.parametrize("url", ["/view_all_available", "/reservation/end?id=1&lat=0&lng=0",
                                 "/search?lat=0&lng=0&radius=1", "/reservation/start?id=1"])
def test_corrupt_db_is_storage_error_on_every_endpoint(client, db_path, content, url):
    # Before PR 7 a corrupt file on /reservation/end was reported as invalid_number
    # ("Lat/Lng values must be numbers"), because json's ValueError was caught there.
    db_path.write_text(content)
    body = assert_envelope(client.get(url), FailureKind.STORAGE_ERROR)
    assert body["msg"].startswith("Error 500 - The scooter database is corrupt")


def test_storage_error_log_has_path_and_cause_but_response_does_not(client, monkeypatch, tmp_path, caplog):
    missing = tmp_path / "nowhere" / "scooters.json"
    monkeypatch.setenv("SCOOTER_DB_PATH", str(missing))
    body = assert_envelope(client.get("/view_all_available"), FailureKind.STORAGE_ERROR)
    assert str(missing) not in body["msg"] and "nowhere" not in body["msg"]
    record = next(r for r in caplog.records if r.getMessage().startswith("storage error:"))
    assert f"(db={missing})" in record.getMessage()
    assert record.exc_info and isinstance(record.exc_info[1], FileNotFoundError)
    assert "Traceback" in caplog.text and "FileNotFoundError" in caplog.text


_GOOD = {"id": "1", "lat": 1.5, "lng": 2, "is_reserved": False}


def _entry(**changes):
    entry = dict(_GOOD, **changes)
    return [{k: v for k, v in entry.items() if v is not ...}]


@pytest.mark.parametrize("data,why", [
    ({}, "top level is dict"),
    ([], None),                                                    # empty list is a valid, empty DB
    ([1], "entry 0 is int"),
    (_entry(id=1), "id is int"),
    (_entry(lat="x"), "lat is not a finite number"),
    (_entry(lng=None), "lng is not a finite number"),
    (_entry(lat=True), "lat is not a finite number"),              # bool is not a coordinate
    (_entry(is_reserved="yes"), "is_reserved is str"),
    (_entry(is_reserved=1), "is_reserved is int"),
    (_entry(is_reserved=...), "missing ['is_reserved']"),
    (_entry(extra="tolerated"), None),                             # extra fields are fine
])
def test_db_shape_is_validated(client, db_path, data, why):
    db_path.write_text(json.dumps(data))
    resp = client.get("/view_all_available")
    if why is None:
        assert resp.status_code == 200
    else:
        body = assert_envelope(resp, FailureKind.STORAGE_ERROR)
        assert body["msg"].startswith("Error 500 - The scooter database is corrupt (") and why in body["msg"]


def test_non_finite_coordinate_in_db_is_storage_error(client, db_path):
    db_path.write_text('[{"id": "1", "lat": NaN, "lng": 0, "is_reserved": false}]')  # json accepts NaN
    body = assert_envelope(client.get("/search?lat=0&lng=0&radius=1"), FailureKind.STORAGE_ERROR)
    assert "lat is not a finite number" in body["msg"]


def test_bad_lat_in_db_is_storage_error_not_internal_error(client, db_path):
    # Before validation this crashed inside geopy and was reported as internal_error.
    db_path.write_text(json.dumps(_entry(lat="x")))
    assert_envelope(client.get("/search?lat=0&lng=0&radius=1"), FailureKind.STORAGE_ERROR)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_unwritable_db_is_storage_error(client, db_path, read_db):
    before = db_path.read_text()
    db_path.chmod(0o444)
    try:
        body = assert_envelope(client.get("/reservation/start?id=18"), FailureKind.STORAGE_ERROR)
        assert body["msg"] == "Error 500 - The scooter database could not be written (PermissionError)."
        assert db_path.read_text() == before
    finally:
        db_path.chmod(0o644)


# ---- payment (was a strict xfail: KeyError -> HTML 500) -------------------------------

def test_failed_payment_is_payment_failed_and_leaves_ride_open(client, monkeypatch, read_db):
    # Today's contract (PLAN PR 7): a gateway failure is reported as payment_failed (502),
    # the ride is NOT ended and nothing is charged. PLAN PRs 13/14 replace the gateway and
    # define decline/timeout outcomes (H3/H7); they will update this test.
    calls = []
    monkeypatch.setattr(app_module, "payment_gateway", failing_gateway(calls))

    start = client.get("/reservation/start?id=18")
    reservation_id = start.get_json()["reservation_id"]
    assert start.status_code == 200 and start.get_json() == {"msg": reserved_ok("18"), "reservation_id": reservation_id}
    before = read_db()
    assert before["18"] == seed_18_reserved(reservation_id, before["18"])

    resp = client.get("/reservation/end?id=18&lat=1&lng=1")
    assert len(calls) == 1, "the payment gateway must actually be called"
    body = assert_envelope(resp, FailureKind.PAYMENT_FAILED)
    assert body["msg"] == "Error 502 - Payment failed; the reservation was not ended."
    assert body["msg"] != no_reservation("18") and "txn_id" not in body

    after = read_db()
    assert after == before                                   # still reserved (same reservation), not moved


def test_gateway_supplied_msg_and_code_are_passed_through(client, monkeypatch):
    monkeypatch.setattr(app_module, "payment_gateway",
                        lambda cost: {"status": False, "msg": "Error 422 - Card declined.", "code": 422})
    client.get("/reservation/start?id=18")
    body = assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.PAYMENT_FAILED, status=422)
    assert body["msg"] == "Error 422 - Card declined."


@pytest.mark.parametrize("code,expected", [
    (422, 422), (503, 503), (599, 599),          # real error codes pass through
    (200, 502), (302, 502), (399, 502), (600, 502), (0, 502), (-1, 502),
    ("422", 502), (True, 502), (None, 502), (422.0, 502),
])
def test_gateway_code_is_clamped_to_an_error_status(client, monkeypatch, code, expected):
    monkeypatch.setattr(app_module, "payment_gateway", lambda cost: {"status": False, "code": code})
    client.get("/reservation/start?id=18")
    assert_envelope(client.get("/reservation/end?id=18&lat=1&lng=1"), FailureKind.PAYMENT_FAILED, status=expected)


def test_error_bodies_are_plain_json_objects(client):
    raw = client.get("/reservation/start?id=nope").get_data(as_text=True)
    assert json.loads(raw) == {"msg": "Error 422 - No scooter with id nope was found.",
                               "error": {"kind": "scooter_not_found"}}
