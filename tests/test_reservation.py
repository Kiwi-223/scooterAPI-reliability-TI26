"""GET /reservation/start?id= and GET /reservation/end?id=&lat=&lng=."""
import pytest

from messages import (err, LAT_RANGE, LNG_RANGE, MISSING_PARAMS, NOT_A_NUMBER, already_reserved,
                      ended_ok, no_reservation, not_found, reserved_ok)


def available_ids(get_json):
    return [s["id"] for s in get_json("/view_all_available", 200)]


# ---- start -------------------------------------------------------------------------

def test_start_reserves_scooter_and_persists_it(get_json, read_db):
    assert get_json("/reservation/start?id=18", 200) == {"msg": reserved_ok("18")}
    assert read_db()["18"]["is_reserved"] is True
    # Nothing else about the scooter changes.
    assert read_db()["18"] == {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": True}


def test_reserved_scooter_disappears_from_available_and_search(get_json):
    get_json("/reservation/start?id=18", 200)
    assert "18" not in available_ids(get_json)
    assert get_json("/search?lat=23&lng=19&radius=10", 200) == []


def test_start_twice_is_rejected(get_json, read_db):
    get_json("/reservation/start?id=18", 200)
    assert get_json("/reservation/start?id=18", 422) == err(already_reserved("18"), "scooter_already_reserved")
    assert read_db()["18"]["is_reserved"] is True


def test_start_on_seed_reserved_scooter_is_rejected(get_json):
    assert get_json("/reservation/start?id=33", 422) == err(already_reserved("33"), "scooter_already_reserved")


@pytest.mark.parametrize("sid", ["nope", ""])
def test_start_unknown_id(get_json, read_db, sid):
    before = read_db()
    assert get_json(f"/reservation/start?id={sid}", 422) == err(not_found(sid), "scooter_not_found")
    assert read_db() == before


def test_start_id_is_matched_exactly(get_json, read_db):
    # "18 " (stray whitespace) is not "18": ids are not trimmed or normalised.
    assert get_json("/reservation/start?id=18%20", 422) == err(not_found("18 "), "scooter_not_found")
    assert read_db()["18"]["is_reserved"] is False


def test_start_missing_id(get_json):
    assert get_json("/reservation/start", 422) == err(MISSING_PARAMS, "missing_param")


# ---- end ---------------------------------------------------------------------------

def test_end_frees_scooter_at_new_location_and_persists_it(get_json, read_db):
    get_json("/reservation/start?id=18", 200)
    body = get_json("/reservation/end?id=18&lat=23.01&lng=19.01", 200)
    assert body["msg"] == ended_ok("18")
    assert "txn_id" in body  # its type is a known bug, see test_known_bugs.py
    assert read_db()["18"] == {"id": "18", "lat": 23.01, "lng": 19.01, "is_reserved": False}


def test_ended_scooter_is_available_at_new_location(get_json):
    get_json("/reservation/start?id=18", 200)
    get_json("/reservation/end?id=18&lat=23.01&lng=19.01", 200)
    available = {s["id"]: s for s in get_json("/view_all_available", 200)}
    assert available["18"] == {"id": "18", "lat": 23.01, "lng": 19.01, "is_reserved": False}
    assert [s["id"] for s in get_json("/search?lat=23.01&lng=19.01&radius=1", 200)] == ["18"]
    assert get_json("/search?lat=23&lng=19&radius=10", 200) == []


def test_end_seed_reserved_scooter(get_json):
    assert get_json("/reservation/end?id=33&lat=24&lng=-28", 200)["msg"] == ended_ok("33")
    assert "33" in available_ids(get_json)


def test_end_when_not_reserved_is_rejected(get_json, read_db):
    before = read_db()
    assert get_json("/reservation/end?id=18&lat=1&lng=1", 422) == err(no_reservation("18"), "no_active_reservation")
    assert read_db() == before


def test_end_twice_is_rejected(get_json, read_db):
    get_json("/reservation/start?id=18", 200)
    get_json("/reservation/end?id=18&lat=23.01&lng=19.01", 200)
    assert get_json("/reservation/end?id=18&lat=5&lng=5", 422) == err(no_reservation("18"), "no_active_reservation")
    assert read_db()["18"]["lat"] == 23.01  # second call did not move it


def test_end_id_is_matched_exactly(get_json, read_db):
    assert get_json("/reservation/end?id=33%20&lat=0&lng=0", 422) == err(not_found("33 "), "scooter_not_found")
    assert read_db()["33"]["is_reserved"] is True


def test_end_unknown_id(get_json):
    assert get_json("/reservation/end?id=nope&lat=0&lng=0", 422) == err(not_found("nope"), "scooter_not_found")


@pytest.mark.parametrize("query", ["lat=0&lng=0", "id=33&lng=0", "id=33&lat=0", ""])
def test_end_missing_param(get_json, read_db, query):
    assert get_json(f"/reservation/end?{query}", 422) == err(MISSING_PARAMS, "missing_param")
    assert read_db()["33"]["is_reserved"] is True


@pytest.mark.parametrize("query", ["id=33&lat=x&lng=0", "id=33&lat=0&lng=x"])
def test_end_non_numeric_coordinate(get_json, read_db, query):
    assert get_json(f"/reservation/end?{query}", 422) == err(NOT_A_NUMBER, "invalid_number")
    assert read_db()["33"]["is_reserved"] is True


@pytest.mark.parametrize("lat,lng", [("90", "180"), ("-90", "-180")])
def test_end_accepts_coordinate_bounds(get_json, read_db, lat, lng):
    assert get_json(f"/reservation/end?id=33&lat={lat}&lng={lng}", 200)["msg"] == ended_ok("33")
    assert read_db()["33"] == {"id": "33", "lat": float(lat), "lng": float(lng), "is_reserved": False}


@pytest.mark.parametrize("lat", ["-91", "90.5"])
def test_end_latitude_out_of_range(get_json, read_db, lat):
    assert get_json(f"/reservation/end?id=33&lat={lat}&lng=0", 422) == err(LAT_RANGE, "invalid_coordinate")
    assert read_db()["33"]["is_reserved"] is True


@pytest.mark.parametrize("lng", ["-181", "180.5"])
def test_end_longitude_out_of_range(get_json, read_db, lng):
    assert get_json(f"/reservation/end?id=33&lat=0&lng={lng}", 422) == err(LNG_RANGE, "invalid_coordinate")
    assert read_db()["33"]["is_reserved"] is True
