"""GET /search?lat=&lng=&radius= (radius in metres)."""
import pytest

from messages import (err, INVALID_RADIUS, LAT_RANGE, LNG_RANGE, MISSING_PARAMS, NOT_A_NUMBER,
                      RADIUS_NOT_A_NUMBER)


def ids(results):
    return [s["id"] for s in results]


def test_search_returns_id_lat_lng_of_scooters_in_radius(get_json):
    assert get_json("/search?lat=0&lng=0&radius=10", 200) == [{"id": "14", "lat": 0, "lng": 0}]


def test_search_radius_is_inclusive(get_json):
    # Scooter 14 is exactly at (0, 0): distance 0 <= radius 0.
    assert ids(get_json("/search?lat=0&lng=0&radius=0", 200)) == ["14"]


def test_search_radius_is_in_metres(get_json):
    # (23.005, 19) is ~553 m north of scooter 18 at (23, 19).
    assert ids(get_json("/search?lat=23.005&lng=19&radius=700", 200)) == ["18"]
    assert get_json("/search?lat=23.005&lng=19&radius=400", 200) == []


def test_search_with_nothing_in_range_returns_empty_list(get_json):
    assert get_json("/search?lat=60&lng=60&radius=1000", 200) == []


def test_search_never_returns_reserved_scooters(get_json):
    # Seed scooter 33 at (24, -28) is reserved.
    assert get_json("/search?lat=24&lng=-28&radius=1000", 200) == []
    # A radius larger than half the Earth's circumference covers every scooter.
    assert ids(get_json("/search?lat=0&lng=0&radius=21000000", 200)) == ["15", "18", "9", "14"]


@pytest.mark.parametrize("lat,lng", [("90", "180"), ("-90", "-180")])
def test_search_accepts_coordinate_bounds(get_json, lat, lng):
    assert get_json(f"/search?lat={lat}&lng={lng}&radius=1", 200) == []


@pytest.mark.parametrize("query", [
    "lng=0&radius=1",
    "lat=0&radius=1",
    "lat=0&lng=0",
    "",
])
def test_search_missing_param(get_json, query):
    assert get_json(f"/search?{query}", 422) == err(MISSING_PARAMS, "missing_param")


@pytest.mark.parametrize("query", [
    "lat=abc&lng=0&radius=1",
    "lat=0&lng=abc&radius=1",
])
def test_search_non_numeric_param(get_json, query):
    assert get_json(f"/search?{query}", 422) == err(NOT_A_NUMBER, "invalid_number")


@pytest.mark.parametrize("lat", ["90.0001", "-91"])
def test_search_latitude_out_of_range(get_json, lat):
    assert get_json(f"/search?lat={lat}&lng=0&radius=1", 422) == err(LAT_RANGE, "invalid_coordinate")


@pytest.mark.parametrize("lng", ["180.5", "-181"])
def test_search_longitude_out_of_range(get_json, lng):
    assert get_json(f"/search?lat=0&lng={lng}&radius=1", 422) == err(LNG_RANGE, "invalid_coordinate")


@pytest.mark.parametrize("radius", ["abc", "", "%20", "1km", "0x10"])
def test_search_non_numeric_radius(get_json, radius):
    # Before PLAN PR 8 this said "Lat/Lng values must be numbers"; the kind is unchanged.
    assert get_json(f"/search?lat=0&lng=0&radius={radius}", 422) == err(RADIUS_NOT_A_NUMBER, "invalid_number")


@pytest.mark.parametrize("radius", ["-5", "-0.001", "-1e-300", "inf", "-inf", "Infinity", "nan", "NaN", "1e309", "-1e309"])
def test_search_rejects_negative_or_non_finite_radius(get_json, radius):
    assert get_json(f"/search?lat=0&lng=0&radius={radius}", 422) == err(INVALID_RADIUS, "invalid_radius")


@pytest.mark.parametrize("radius,expected", [
    ("0", ["14"]), ("-0", ["14"]), ("-0.0", ["14"]), ("0.0", ["14"]),
    ("1e-300", ["14"]),
    ("1e300", ["15", "18", "9", "14"]),        # no upper bound: a huge radius covers every scooter
])
def test_search_accepts_zero_and_any_finite_non_negative_radius(get_json, radius, expected):
    assert ids(get_json(f"/search?lat=0&lng=0&radius={radius}", 200)) == expected


@pytest.mark.parametrize("query,expected", [
    # Validation order is unchanged: lat/lng parse, radius parse, coordinate range, radius range.
    ("lat=abc&lng=0&radius=abc", err(NOT_A_NUMBER, "invalid_number")),
    ("lat=abc&lng=0", err(NOT_A_NUMBER, "invalid_number")),            # bad lat beats missing radius
    ("lat=0&lng=0&radius=abc", err(RADIUS_NOT_A_NUMBER, "invalid_number")),
    ("lat=91&lng=0&radius=abc", err(RADIUS_NOT_A_NUMBER, "invalid_number")),
    ("lat=91&lng=0&radius=-1", err(LAT_RANGE, "invalid_coordinate")),
    ("lat=0&lng=0&radius=-1&radius=5", err(INVALID_RADIUS, "invalid_radius")),  # first value wins
])
def test_search_error_precedence(get_json, query, expected):
    assert get_json(f"/search?{query}", 422) == expected

