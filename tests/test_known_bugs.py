"""Known bugs, pinned as strict xfails.

Each test asserts the behaviour the named PLAN.md PR will deliver. Today it fails with the
given exception, so pytest reports XFAIL. When the PR fixes the bug, the test passes,
strict mode turns that XPASS into a failure, and the PR must remove the xfail marker
(and usually tighten the assertion to the final contract).

None are pinned right now. The last one (PLAN PR 14: "with an unwritable DB, end-ride
charges first and then fails to save") is fixed and lives on as
tests/test_end_ride.py::test_no_charge_when_the_ride_cannot_be_ended.
"""
