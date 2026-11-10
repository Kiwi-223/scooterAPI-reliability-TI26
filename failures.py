"""Stable failure kinds: the fixed set of ways a request can fail.

Every error response carries one of these ids as `error.kind`:

    {"msg": "<human-readable message>", "error": {"kind": "<id>"}}

The ids are a public contract (README.md, AGENTS.md). Once an id has shipped it is never
renamed or reused for a different meaning. A new failure mode gets a new id, and a test in
tests/test_failure_kinds.py must exercise it through a real request (the test fails if a
kind has no request).
"""
from enum import StrEnum
from http import HTTPStatus


class FailureKind(StrEnum):
    """A failure kind: its stable id (the enum value), default HTTP status and meaning."""

    def __new__(cls, kind_id, status, description):
        member = str.__new__(cls, kind_id)
        member._value_ = kind_id
        member.status = int(status)
        member.description = description
        return member

    # ---- request validation (422) ------------------------------------------------------
    MISSING_PARAM = ("missing_param", HTTPStatus.UNPROCESSABLE_ENTITY,
                     "A required query parameter is missing.")
    INVALID_NUMBER = ("invalid_number", HTTPStatus.UNPROCESSABLE_ENTITY,
                      "lat, lng or radius is not a number.")
    INVALID_COORDINATE = ("invalid_coordinate", HTTPStatus.UNPROCESSABLE_ENTITY,
                          "Latitude is outside [-90, 90] or longitude outside [-180, 180].")
    INVALID_RADIUS = ("invalid_radius", HTTPStatus.UNPROCESSABLE_ENTITY,
                      "radius is negative or not finite (inf, -inf, nan); it must be >= 0 metres.")

    # ---- scooter / reservation state (422) ---------------------------------------------
    SCOOTER_NOT_FOUND = ("scooter_not_found", HTTPStatus.UNPROCESSABLE_ENTITY,
                         "No scooter has the given id.")
    SCOOTER_ALREADY_RESERVED = ("scooter_already_reserved", HTTPStatus.UNPROCESSABLE_ENTITY,
                                "Start was requested for a scooter that is already reserved.")
    NO_ACTIVE_RESERVATION = ("no_active_reservation", HTTPStatus.UNPROCESSABLE_ENTITY,
                             "End was requested for a scooter that is not reserved.")
    SCOOTER_PAYMENT_PENDING = ("scooter_payment_pending", HTTPStatus.UNPROCESSABLE_ENTITY,
                               "Start was requested for a scooter whose last ride is waiting for payment "
                               "(payment_pending); retry ending that ride, or wait for reconcile.")

    # ---- payment (H3, H7; AGENTS.md "Payment state machine") ------------------------------
    # Every payment failure body also says whether the ride ended (`ride_ended`).
    # Since PLAN PR 13 payment_failed means "the gateway reported an error" (outcome `error`,
    # or a ledger it can't read). Its id and status are unchanged.
    PAYMENT_FAILED = ("payment_failed", HTTPStatus.BAD_GATEWAY,
                      "The payment gateway reported an error; the ride is payment_pending until a retry settles it.")
    PAYMENT_DECLINED = ("payment_declined", HTTPStatus.UNPROCESSABLE_ENTITY,
                        "The payment was declined; the ride was ended and its cost recorded as an "
                        "outstanding balance (H3). The body has ride_ended: true and outstanding_balance.")
    PAYMENT_TIMEOUT = ("payment_timeout", HTTPStatus.GATEWAY_TIMEOUT,
                       "The payment gateway timed out; the rider may or may not have been charged. "
                       "The ride is payment_pending; a retry settles it and is charged at most once.")
    PAYMENT_CONFLICT = ("payment_conflict", HTTPStatus.CONFLICT,
                        "The reservation's idempotency key already has a charge for a different ride "
                        "(scooter, reservation, end location or amount); nothing was charged and the "
                        "ride is open again (payment_state active).")

    # ---- reconcile (PLAN PR 15) ----------------------------------------------------------
    # Not an API response: recorded on `make reconcile`'s /reconcile records when it leaves a
    # ride as it was for a human (a charge that doesn't fit, an unreadable ledger, a conflict),
    # so `recent-failures` / `failure-counts` show "reconcile couldn't settle scooter 9".
    RECONCILE_UNSETTLED = ("reconcile_unsettled", HTTPStatus.CONFLICT,
                           "make reconcile couldn't safely settle this ride and left it unchanged for a human; "
                           "the reason says why.")

    # ---- framework / infrastructure ----------------------------------------------------
    ROUTE_NOT_FOUND = ("route_not_found", HTTPStatus.NOT_FOUND,
                       "No endpoint exists at this path.")
    METHOD_NOT_ALLOWED = ("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED,
                          "The endpoint exists but not for this HTTP method (all endpoints are GET).")
    STORAGE_ERROR = ("storage_error", HTTPStatus.INTERNAL_SERVER_ERROR,
                     "The scooter database file is missing, unreadable, corrupt or unwritable.")
    # Retryable, unlike storage_error: another request held the DB lock for longer than
    # SCOOTER_DB_LOCK_TIMEOUT. Nothing was read or written; retry after Retry-After.
    STORAGE_BUSY = ("storage_busy", HTTPStatus.SERVICE_UNAVAILABLE,
                    "The scooter database is locked by another request; nothing was changed, retry shortly.")
    INTERNAL_ERROR = ("internal_error", HTTPStatus.INTERNAL_SERVER_ERROR,
                      "Any other unexpected error (a bug); details are in the server log.")


class StorageError(Exception):
    """Raised when the scooter database can't be read, parsed, validated or written.

    `str(error)` is safe to send to clients (no path, no file contents). `path` is for the
    server log only; the original exception is chained as `__cause__` when there is one.
    """

    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = path


class StorageBusy(Exception):
    """Raised when the DB lock can't be acquired within SCOOTER_DB_LOCK_TIMEOUT."""

    def __init__(self, message, path=None, waited_ms=None):
        super().__init__(message)
        self.path = path
        self.waited_ms = waited_ms

