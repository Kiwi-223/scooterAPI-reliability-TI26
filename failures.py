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

    # ---- scooter / reservation state (422) ---------------------------------------------
    SCOOTER_NOT_FOUND = ("scooter_not_found", HTTPStatus.UNPROCESSABLE_ENTITY,
                         "No scooter has the given id.")
    SCOOTER_ALREADY_RESERVED = ("scooter_already_reserved", HTTPStatus.UNPROCESSABLE_ENTITY,
                                "Start was requested for a scooter that is already reserved.")
    NO_ACTIVE_RESERVATION = ("no_active_reservation", HTTPStatus.UNPROCESSABLE_ENTITY,
                             "End was requested for a scooter that is not reserved.")

    # ---- payment ---------------------------------------------------------------------
    # The gateway reported a failure. The ride is NOT ended and the scooter stays reserved
    # (today's behaviour). PLAN PRs 13/14 add finer kinds (decline, timeout, gateway error).
    PAYMENT_FAILED = ("payment_failed", HTTPStatus.BAD_GATEWAY,
                      "The payment gateway reported a failure; the reservation was not ended.")

    # ---- framework / infrastructure ----------------------------------------------------
    ROUTE_NOT_FOUND = ("route_not_found", HTTPStatus.NOT_FOUND,
                       "No endpoint exists at this path.")
    METHOD_NOT_ALLOWED = ("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED,
                          "The endpoint exists but not for this HTTP method (all endpoints are GET).")
    STORAGE_ERROR = ("storage_error", HTTPStatus.INTERNAL_SERVER_ERROR,
                     "The scooter database file is missing, unreadable, corrupt or unwritable.")
    INTERNAL_ERROR = ("internal_error", HTTPStatus.INTERNAL_SERVER_ERROR,
                      "Any other unexpected error (a bug); details are in the server log.")


class StorageError(Exception):
    """Raised when the scooter database can't be read or written."""
