"""Known bugs, pinned as strict xfails.

Each test asserts the behaviour the named PLAN.md PR will deliver. Today it fails with the
given exception, so pytest reports XFAIL. When the PR fixes the bug, the test passes,
strict mode turns that XPASS into a failure, and the PR must remove the xfail marker
(and usually tighten the assertion to the final contract).
"""
import os

import pytest


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
@pytest.mark.xfail(raises=AssertionError, reason="PLAN PR 14: with an unwritable DB, end-ride charges first and then fails to save, so the rider pays for a ride that did not end")
def test_no_charge_when_the_ride_cannot_be_ended(client, db_path, read_db, read_ledger):
    # PR 14's invariant: no charge without an ended ride. It persists payment_pending
    # BEFORE calling the gateway, so an unwritable DB fails the request before any charge.
    assert client.get("/reservation/start?id=18").status_code == 200
    before = read_db()
    db_path.chmod(0o444)
    try:
        resp = client.get("/reservation/end?id=18&lat=1&lng=1")
    finally:
        db_path.chmod(0o644)
    # Preconditions that hold today too, so the xfail can only flip for the right reason:
    assert resp.status_code == 500 and resp.get_json()["error"]["kind"] == "storage_error"
    assert read_db() == before and before["18"]["is_reserved"] is True   # ride not ended
    # The bug: the gateway was called (PR 13's ledger shows it) and charged anyway.
    charges = [e for e in read_ledger() if e["charged"]]
    assert len(read_ledger()) == 1, "the gateway must actually have been called"
    assert charges == [], f"charged {charges} for a ride that did not end"

