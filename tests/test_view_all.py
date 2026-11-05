"""GET / and GET /view_all_available."""

AVAILABLE_IN_SEED = [
    {"id": "15", "lat": 0.0, "lng": -129.0, "is_reserved": False},
    {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": False},
    {"id": "9", "lat": -14, "lng": -153, "is_reserved": False},
    {"id": "14", "lat": 0, "lng": 0, "is_reserved": False},
]


def test_root_redirects_to_view_all_available(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/view_all_available")


def test_root_followed_returns_available_scooters(client):
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.get_json() == AVAILABLE_IN_SEED


def test_view_all_available_lists_only_unreserved_scooters(get_json):
    # Seed scooter 33 is reserved, so it must not be listed.
    assert get_json("/view_all_available", 200) == AVAILABLE_IN_SEED
