"""Known bugs, pinned as strict xfails.

Each test asserts the behaviour the named PLAN.md PR will deliver. Today it fails with the
given exception, so pytest reports XFAIL. When the PR fixes the bug, the test passes,
strict mode turns that XPASS into a failure, and the PR must remove the xfail marker
(and usually tighten the assertion to the final contract).
"""
import pytest


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 8: radius=inf is accepted and returns every scooter")
def test_infinite_radius_is_rejected(client):
    assert client.get("/search?lat=0&lng=0&radius=inf").status_code == 422


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 8: negative radius is accepted")
def test_negative_radius_is_rejected(client):
    assert client.get("/search?lat=0&lng=0&radius=-5").status_code == 422


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 8: radius=nan is accepted (returns [])")
def test_nan_radius_is_rejected(client):
    assert client.get("/search?lat=0&lng=0&radius=nan").status_code == 422


@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 13: txn_id is an int; README documents a string")
def test_txn_id_is_a_string(client):
    client.get("/reservation/start?id=18")
    assert isinstance(client.get("/reservation/end?id=18&lat=1&lng=1").get_json()["txn_id"], str)
