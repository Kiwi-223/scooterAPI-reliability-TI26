"""Known bugs, pinned as strict xfails.

Each test asserts the behaviour the named PLAN.md PR will deliver. Today it fails with the
given exception, so pytest reports XFAIL. When the PR fixes the bug, the test passes,
strict mode turns that XPASS into a failure, and the PR must remove the xfail marker
(and usually tighten the assertion to the final contract).
"""
import pytest

from messages import no_reservation, reserved_ok

SEED_18 = {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": True}  # after start
END_18 = {"id": "18", "lat": 1.0, "lng": 1.0, "is_reserved": False}    # freed at end location


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 8: radius=inf is accepted and returns every scooter")
def test_infinite_radius_is_rejected(client):
    assert client.get("/search?lat=0&lng=0&radius=inf").status_code == 422


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 8: negative radius is accepted")
def test_negative_radius_is_rejected(client):
    assert client.get("/search?lat=0&lng=0&radius=-5").status_code == 422


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 8: radius=nan is accepted (returns [])")
def test_nan_radius_is_rejected(client):
    assert client.get("/search?lat=0&lng=0&radius=nan").status_code == 422


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 7: Flask's 404 page is HTML, not JSON")
def test_unknown_route_returns_json(client):
    resp = client.get("/no-such-route")
    assert resp.status_code == 404
    assert resp.mimetype == "application/json"


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 7: Flask's 405 page is HTML, not JSON")
def test_wrong_method_returns_json(client):
    resp = client.post("/search")
    assert resp.status_code == 405
    assert resp.mimetype == "application/json"


@pytest.mark.xfail(raises=KeyError, reason="PLAN PR 13: a failed payment crashes end_reservation with KeyError 'msg'")
def test_failed_payment_returns_json_error(client, monkeypatch, read_db):
    # Each precondition is asserted, so the test can't pass for the wrong reason
    # (e.g. start not persisting -> end answers "no reservation" without paying).
    import app as app_module
    gateway_calls = []

    def failing_gateway(cost):
        gateway_calls.append(cost)
        return {"status": False}

    monkeypatch.setattr(app_module, "payment_gateway", failing_gateway)
    before = read_db()

    start = client.get("/reservation/start?id=18")
    assert start.status_code == 200 and start.get_json() == {"msg": reserved_ok("18")}
    assert read_db()["18"] == SEED_18

    resp = client.get("/reservation/end?id=18&lat=1&lng=1")
    assert len(gateway_calls) == 1, "the payment gateway must actually be called"
    assert resp.status_code != 500
    assert resp.mimetype == "application/json"
    body = resp.get_json()
    assert body.get("msg") != no_reservation("18")
    assert "txn_id" not in body, "a failed payment must not report a transaction"

    # Nothing is half-written: the scooter is either still reserved where it was (payment
    # pending) or freed at the end location (H3 decline policy, PLAN PR 14). Never moved
    # but still reserved, or freed at the old location. Other scooters are untouched.
    after = read_db()
    assert after["18"] in (SEED_18, END_18)
    assert {k: v for k, v in after.items() if k != "18"} == {k: v for k, v in before.items() if k != "18"}


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 13: txn_id is an int; README documents a string")
def test_txn_id_is_a_string(client):
    client.get("/reservation/start?id=18")
    assert isinstance(client.get("/reservation/end?id=18&lat=1&lng=1").get_json()["txn_id"], str)
