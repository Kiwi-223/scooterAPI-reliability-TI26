"""Persisted reservation state (PLAN PR 12).

Covers the DB schema (AGENTS.md "Database schema"): the `reservation` object and the
invariant `is_reserved == (reservation is not null)`, loading legacy DBs that predate it,
the reservation lifecycle across start / end / re-reserve, the start response's
`reservation_id`, and the DB validation deferred to this PR (coordinate ranges, duplicate
ids, unknown fields kept on write).
"""
import datetime
import json
import re
import shutil
from pathlib import Path

import pytest

import app as app_module
import records_query as rq
from failures import FailureKind
from messages import already_reserved, ended_ok, err, reserved_ok
from test_failure_kinds import assert_envelope

REPO = Path(__file__).resolve().parent.parent
SEED_DB = Path(__file__).resolve().parent / "seed_db.json"
HEX32 = re.compile(r"[0-9a-f]{32}")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
PUBLIC_FIELDS = {"id", "lat", "lng", "is_reserved"}

# The exact bytes of the committed scooter_db.json when PLAN PR 12 landed: the legacy shape
# (no "reservation" key) with scooter 33 reserved. tests/seed_db.json holds the same data.
LEGACY_DB_TEXT = (
    '[{"id": "15", "lat": 0.0, "lng": -129.0, "is_reserved": false}, '
    '{"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": false}, '
    '{"id": "9", "lat": -14, "lng": -153, "is_reserved": false}, '
    '{"id": "14", "lat": 0, "lng": 0, "is_reserved": false}, '
    '{"id": "33", "lat": 24.0, "lng": -28.0, "is_reserved": true}]'
)
LEGACY_33 = {"reservation_id": "legacy-33", "started_at": None, "start_request_id": None,
             "payment_state": "active", "idempotency_key": "legacy-33", "legacy": True}
A_RESERVATION = {"reservation_id": "a" * 32, "started_at": "2026-11-09T12:00:00.000Z",
                 "start_request_id": "req-1", "payment_state": "active", "idempotency_key": "a" * 32}


def seed():
    return json.loads(SEED_DB.read_text())


def write(db_path, data):
    db_path.write_text(json.dumps(data))


def by_id(data):
    return {s["id"]: s for s in data}


# ---- legacy DBs (no "reservation" key) ----------------------------------------------------

@pytest.fixture
def legacy_db(db_path):
    db_path.write_text(LEGACY_DB_TEXT)
    return db_path


def test_the_legacy_fixture_is_todays_seed():
    assert json.loads(LEGACY_DB_TEXT) == seed()
    assert all("reservation" not in s for s in seed())          # the test seed stays legacy-shaped


def test_the_committed_db_loads(db_path):
    # Whatever shape the repo's scooter_db.json is in (legacy, or migrated by a manual run),
    # the app must load it. Read a copy: tests never touch the live file.
    shutil.copyfile(REPO / "scooter_db.json", db_path)
    assert len(app_module.init_db(str(db_path))) == len(json.loads((REPO / "scooter_db.json").read_text()))


def test_legacy_db_serves_reads_unchanged(get_json, legacy_db):
    available = get_json("/view_all_available", 200)
    assert available == [s for s in json.loads(LEGACY_DB_TEXT) if not s["is_reserved"]]
    assert [s["id"] for s in get_json("/search?lat=0&lng=0&radius=10", 200)] == ["14"]
    assert legacy_db.read_text() == LEGACY_DB_TEXT                # reads never write


def test_legacy_reserved_scooter_loads_with_a_deterministic_legacy_reservation(legacy_db):
    first, second = app_module.init_db(str(legacy_db)), app_module.init_db(str(legacy_db))
    assert by_id(s.to_dict() for s in first)["33"]["reservation"] == LEGACY_33
    assert [s.to_dict() for s in first] == [s.to_dict() for s in second]
    assert all(s.reservation is None for s in first if s.id != "33")


def test_legacy_reserved_scooter_stays_reserved(get_json, legacy_db):
    assert get_json("/reservation/start?id=33", 422) == err(already_reserved("33"), "scooter_already_reserved")
    assert legacy_db.read_text() == LEGACY_DB_TEXT


def test_ending_a_legacy_reservation(client, legacy_db, read_db, read_records):
    resp = client.get("/reservation/end?id=33&lat=24.5&lng=-28.5")
    assert resp.status_code == 200 and resp.get_json()["msg"] == ended_ok("33")
    assert read_db()["33"] == {"id": "33", "lat": 24.5, "lng": -28.5, "is_reserved": False, "reservation": None}
    r = read_records()[-1]
    assert r["scooter_before"]["reservation"] == LEGACY_33
    cleared = [d for d in r["decisions"] if d["step"] == "reservation"]
    assert cleared == [{"step": "reservation", "outcome": "cleared",
                        "detail": {"reservation_id": "legacy-33", "legacy": True}}]
    # ... and the scooter can then be reserved normally
    assert client.get("/reservation/start?id=33").status_code == 200
    assert HEX32.fullmatch(read_db()["33"]["reservation"]["reservation_id"])
    assert rq.anomalies(read_records()) == []


def test_the_first_write_migrates_every_entry(get_json, legacy_db, read_db):
    get_json("/reservation/start?id=18", 200)
    db = read_db()
    assert all(set(s) == PUBLIC_FIELDS | {"reservation"} for s in db.values())
    assert db["33"]["reservation"] == LEGACY_33                   # kept, marked legacy
    assert [db[sid]["reservation"] for sid in ("15", "9", "14")] == [None, None, None]
    assert app_module.find_db_shape_problem(list(db.values())) is None
    assert get_json("/view_all_available", 200)                   # and it loads again


# ---- lifecycle -----------------------------------------------------------------------------

def test_start_creates_and_persists_a_reservation(client, read_db, read_records):
    before = datetime.datetime.now(datetime.UTC)
    resp = client.get("/reservation/start?id=18", headers={"X-Request-ID": "probe-start-1"})
    after = datetime.datetime.now(datetime.UTC)
    body = resp.get_json()
    assert resp.status_code == 200 and body == {"msg": reserved_ok("18"), "reservation_id": body["reservation_id"]}
    reservation = read_db()["18"]["reservation"]
    assert set(reservation) == app_module.RESERVATION_FIELDS
    assert HEX32.fullmatch(reservation["reservation_id"]) and reservation["reservation_id"] == body["reservation_id"]
    assert reservation["idempotency_key"] == reservation["reservation_id"]
    assert reservation["payment_state"] == "active"
    assert reservation["start_request_id"] == "probe-start-1" == resp.headers["X-Request-ID"]
    assert TS_RE.fullmatch(reservation["started_at"])
    started = datetime.datetime.fromisoformat(reservation["started_at"])
    assert before - datetime.timedelta(milliseconds=1) <= started <= after
    assert read_records()[-1]["scooter_after"]["reservation"] == reservation


def test_end_clears_and_re_reserving_creates_a_new_reservation(client, read_db, read_records):
    first = client.get("/reservation/start?id=18").get_json()["reservation_id"]
    assert client.get("/reservation/end?id=18&lat=23.01&lng=19.01").status_code == 200
    assert read_db()["18"]["reservation"] is None and read_db()["18"]["is_reserved"] is False
    resp = client.get("/reservation/start?id=18")
    second = resp.get_json()["reservation_id"]
    assert HEX32.fullmatch(second) and second != first
    assert read_db()["18"]["reservation"]["start_request_id"] == resp.headers["X-Request-ID"]
    assert rq.anomalies(read_records()) == []


def test_refused_start_keeps_the_existing_reservation(client, read_db):
    client.get("/reservation/start?id=18")
    before = read_db()
    assert client.get("/reservation/start?id=18").status_code == 422
    assert read_db() == before


def test_starting_one_scooter_keeps_the_others_reservations(client, read_db):
    first = client.get("/reservation/start?id=18").get_json()["reservation_id"]
    client.get("/reservation/start?id=15")
    assert read_db()["18"]["reservation"]["reservation_id"] == first
    assert read_db()["33"]["reservation"] == LEGACY_33


def test_view_all_only_shows_the_public_fields(client, db_path, get_json):
    data = seed()
    data[0]["battery"] = 80
    write(db_path, data)
    client.get("/reservation/start?id=18")
    available = get_json("/view_all_available", 200)
    assert available and all(set(s) == PUBLIC_FIELDS for s in available)


def test_a_write_that_would_break_the_invariant_is_refused(client, monkeypatch, db_path, read_records):
    real = app_module.convert_db_to_dictlist

    def forgets_the_reservation(db):
        rows = real(db)
        for row in rows:
            if row["id"] == "18":
                row["reservation"] = None                        # reserved without a reservation
        return rows
    monkeypatch.setattr(app_module, "convert_db_to_dictlist", forgets_the_reservation)
    before = db_path.read_bytes()
    assert_envelope(client.get("/reservation/start?id=18"), FailureKind.INTERNAL_ERROR)
    assert db_path.read_bytes() == before                        # the DB is still loadable
    r = read_records()[-1]
    assert r["db_write"] == {"attempted": True, "ok": False, "error": "InvalidDbState"}
    refused = [d for d in r["decisions"] if d["step"] == "db_write"]
    assert refused == [{"step": "db_write", "outcome": "refused",
                        "detail": {"db_path": str(db_path), "error": "entry 1: is_reserved is true but reservation is null"}}]
    assert r["scooter_after"] is None


# ---- schema validation ---------------------------------------------------------------------

def _with(sid, **changes):
    """The seed, with changes to one entry (a value of ... deletes that key)."""
    data = seed()
    entry = next(s for s in data if s["id"] == sid)
    for key, value in changes.items():
        if value is ...:
            entry.pop(key, None)
        else:
            entry[key] = value
    return data


def _with2(*changes):
    """The seed, with changes to several entries: (id, {field: value}) pairs."""
    data = seed()
    for sid, fields in changes:
        next(s for s in data if s["id"] == sid).update(fields)
    return data


def _res(**changes):
    reservation = dict(A_RESERVATION)
    for key, value in changes.items():
        if value is ...:
            reservation.pop(key)
        else:
            reservation[key] = value
    return reservation


@pytest.mark.parametrize("data,why", [
    (_with("33", reservation=None), "entry 4: is_reserved is true but reservation is null"),
    (_with("18", reservation=A_RESERVATION), "entry 1: is_reserved is false but reservation is set"),
    (_with("33", reservation="abc"), "entry 4: reservation is str, not an object or null"),
    (_with("33", reservation=[]), "entry 4: reservation is list, not an object or null"),
    (_with("33", reservation=_res(started_at=...)), "entry 4: reservation is missing ['started_at']"),
    (_with("33", reservation=_res(reservation_id="")), "entry 4: reservation.reservation_id is not a non-empty string"),
    (_with("33", reservation=_res(idempotency_key=5)), "entry 4: reservation.idempotency_key is not a non-empty string"),
    (_with("33", reservation=_res(started_at=5)), "entry 4: reservation.started_at is int, not a string or null"),
    (_with("33", reservation=_res(start_request_id=[])), "entry 4: reservation.start_request_id is list, not a string or null"),
    (_with("33", reservation=_res(payment_state="")), "entry 4: reservation.payment_state is not a non-empty string"),
    (_with("33", reservation=_res(payment_state=None)), "entry 4: reservation.payment_state is not a non-empty string"),
    (_with("33", reservation=_res(payment_state=["active"])), "entry 4: reservation.payment_state is not a non-empty string"),
    (_with("33", reservation=_res(legacy="yes")), "entry 4: reservation.legacy is not a boolean"),
    (_with("15", lat=90.0001), "entry 0: lat is outside [-90, 90]"),
    (_with("15", lat=-91), "entry 0: lat is outside [-90, 90]"),
    (_with("15", lng=180.5), "entry 0: lng is outside [-180, 180]"),
    (_with("15", lng=-181), "entry 0: lng is outside [-180, 180]"),
    (seed() + [dict(seed()[1])], "entry 5: duplicate id (same as entry 1)"),
    (seed() + [dict(seed()[4], lat=1.0)], "entry 5: duplicate id (same as entry 4)"),
    # reservation ids and idempotency keys are unique across scooters (one key, one ride)
    (_with2(("18", dict(is_reserved=True, reservation=A_RESERVATION)), ("33", dict(reservation=A_RESERVATION))),
     "entry 4: duplicate reservation.reservation_id (same as entry 1)"),
    (_with2(("18", dict(is_reserved=True, reservation=A_RESERVATION)),
            ("33", dict(reservation=_res(reservation_id="b" * 32)))),
     "entry 4: duplicate reservation.idempotency_key (same as entry 1)"),
    (_with("18", is_reserved=True, reservation=_res(reservation_id="legacy-33", idempotency_key="k")),
     "entry 4: duplicate reservation.reservation_id (same as entry 1)"),   # vs 33's synthesised legacy id
])
def test_invalid_db_is_storage_error_everywhere(client, db_path, data, why):
    write(db_path, data)
    before = db_path.read_bytes()
    for url in ("/view_all_available", "/search?lat=0&lng=0&radius=1",
                "/reservation/start?id=18", "/reservation/end?id=33&lat=0&lng=0"):
        body = assert_envelope(client.get(url), FailureKind.STORAGE_ERROR)
        assert body["msg"] == f"Error 500 - The scooter database is corrupt ({why})."
    assert db_path.read_bytes() == before


@pytest.mark.parametrize("data", [
    _with("15", lat=90, lng=-180),                              # range boundaries are valid
    _with("15", lat=-90.0, lng=180.0),
    seed() + [dict(seed()[1], id="18 ")],                       # ids are compared exactly
    _with("33", reservation=A_RESERVATION),                     # the new shape
    _with("33", reservation=_res(started_at=None, start_request_id=None)),
    _with("18", reservation=None),                              # mixed legacy/new entries
    _with("33", reservation=dict(LEGACY_33)),
    _with("33", reservation=dict(A_RESERVATION, gateway_ref="x")),   # unknown keys inside are kept
    _with("33", reservation=_res(payment_state="payment_pending")),  # unknown state: loads (scoped below)
    _with2(("18", dict(is_reserved=True, reservation=A_RESERVATION)),
           ("33", dict(reservation=_res(reservation_id="b" * 32, idempotency_key="b" * 32)))),
])
def test_valid_db_shapes_load(get_json, db_path, data):
    write(db_path, data)
    get_json("/view_all_available", 200)


# ---- unknown fields are kept on write --------------------------------------------------------

def test_unknown_fields_round_trip_through_writes(client, db_path, read_db, get_json):
    data = seed()
    data[0]["zone"] = "harbour"                                          # 15
    data[1].update(battery=87, meta={"tags": ["a", "b"], "n": None})     # 18
    data[4]["reservation"] = dict(A_RESERVATION, gateway_ref="g-1")      # 33, new shape + extra key
    write(db_path, data)

    assert client.get("/reservation/start?id=18").status_code == 200
    assert client.get("/reservation/end?id=18&lat=1&lng=2").status_code == 200
    assert client.get("/reservation/start?id=15").status_code == 200
    db = read_db()
    assert db["15"]["zone"] == "harbour"
    assert (db["18"]["battery"], db["18"]["meta"]) == (87, {"tags": ["a", "b"], "n": None})
    assert db["18"]["lat"] == 1.0 and db["18"]["reservation"] is None    # the known fields did change
    assert db["33"]["reservation"] == dict(A_RESERVATION, gateway_ref="g-1")
    assert {k: v for k, v in db["9"].items() if k != "reservation"} == data[2]
    assert all(set(s) == PUBLIC_FIELDS for s in get_json("/view_all_available", 200))


def test_a_write_that_would_duplicate_a_reservation_id_is_refused(client, monkeypatch, db_path, read_records):
    # e.g. a bug that reuses another scooter's reservation: one key for two rides
    monkeypatch.setattr(app_module, "new_reservation", lambda: {k: v for k, v in LEGACY_33.items() if k != "legacy"})
    before = db_path.read_bytes()
    assert_envelope(client.get("/reservation/start?id=18"), FailureKind.INTERNAL_ERROR)
    assert db_path.read_bytes() == before
    r = read_records()[-1]
    assert r["db_write"]["error"] == "InvalidDbState"
    assert [d["detail"]["error"] for d in r["decisions"] if d["step"] == "db_write"] == \
        ["entry 4: duplicate reservation.reservation_id (same as entry 1)"]


# ---- a payment state this code doesn't know (e.g. after a rollback past PLAN PR 13+) ----------

PENDING = dict(A_RESERVATION, payment_state="payment_pending", gateway_ref="g-9")
UNSUPPORTED_MSG = ("Error 500 - The reservation for scooter 33 is in a payment state this server version "
                   "does not handle (reservation.payment_state is not one of ['active']).")


@pytest.fixture
def pending_db(db_path):
    write(db_path, _with("33", reservation=PENDING))
    return db_path


def test_unknown_payment_state_scooter_reads_as_reserved(get_json, pending_db):
    assert "33" not in [s["id"] for s in get_json("/view_all_available", 200)]
    assert len(get_json("/view_all_available", 200)) == 4
    assert get_json("/search?lat=24&lng=-28&radius=1000", 200) == []


def test_other_scooters_keep_working_and_the_unknown_reservation_is_kept(client, pending_db, read_db):
    assert client.get("/reservation/start?id=18").status_code == 200
    assert client.get("/reservation/end?id=18&lat=1&lng=1").status_code == 200
    assert read_db()["33"] == {"id": "33", "lat": 24.0, "lng": -28.0, "is_reserved": True, "reservation": PENDING}


@pytest.mark.parametrize("url", ["/reservation/start?id=33", "/reservation/end?id=33&lat=1&lng=1"])
def test_start_and_end_on_the_unknown_payment_state_scooter_are_refused(client, monkeypatch, pending_db, read_records, url):
    calls = []
    monkeypatch.setattr(app_module, "payment_gateway", lambda cost: calls.append(cost) or {"status": True, "txn_id": 1})
    before = pending_db.read_bytes()
    body = assert_envelope(client.get(url), FailureKind.STORAGE_ERROR)
    assert body["msg"] == UNSUPPORTED_MSG
    assert calls == [] and pending_db.read_bytes() == before            # never charged, nothing written
    r = read_records()[-1]
    assert {"step": "reservation", "outcome": "unsupported",
            "detail": {"reservation_id": "a" * 32, "payment_state": "payment_pending"}} in r["decisions"]
    assert not any(d["step"] in ("payment", "reserve", "end") for d in r["decisions"])
    assert r["db_write"]["attempted"] is False


def test_the_write_guard_never_changes_an_unknown_payment_state_reservation(client, monkeypatch, pending_db, read_records):
    monkeypatch.setattr(app_module, "refuse_unsupported_reservation", lambda scooter, path: None)  # planted bug
    before = pending_db.read_bytes()
    assert_envelope(client.get("/reservation/end?id=33&lat=1&lng=1"), FailureKind.INTERNAL_ERROR)
    assert pending_db.read_bytes() == before
    assert [d["detail"]["error"] for d in read_records()[-1]["decisions"] if d["step"] == "db_write"] == \
        ["entry 4: a reservation in an unknown payment state was changed"]


def test_the_write_guard_compares_against_what_was_read_not_the_live_object(client, monkeypatch, pending_db):
    # A planted bug that "upgrades" the unknown state in place: the guard must compare with a
    # copy taken at load time, or it would see nothing changed.
    def upgrade_in_place(scooter, path):
        scooter.reservation["payment_state"] = "active"
    monkeypatch.setattr(app_module, "refuse_unsupported_reservation", upgrade_in_place)
    before = pending_db.read_bytes()
    assert_envelope(client.get("/reservation/end?id=33&lat=1&lng=1"), FailureKind.INTERNAL_ERROR)
    assert pending_db.read_bytes() == before


def test_the_write_guard_never_creates_an_unknown_payment_state(client, monkeypatch, db_path):
    real = app_module.new_reservation
    monkeypatch.setattr(app_module, "new_reservation", lambda: dict(real(), payment_state="made_up"))
    before = db_path.read_bytes()
    assert_envelope(client.get("/reservation/start?id=18"), FailureKind.INTERNAL_ERROR)
    assert db_path.read_bytes() == before


# ---- restoring a DB snapshot replays its reservations (input for PLAN PR 13) -----------------

@pytest.mark.parametrize("sid", ["18", "33"])                       # a uuid reservation, and a legacy one
def test_a_restored_reservation_is_charged_again_not_ridden_free(client, monkeypatch, db_path, read_records, sid):
    # Restoring a snapshot (git restore, a backup) brings back every reservation in it, with
    # the same reservation_id and idempotency_key. Today there's no ledger, so ending it again
    # charges again. PLAN PR 13's ledger must keep that from becoming a free ride: a ledger
    # hit only counts as "already charged" if it matches the ride; see PLAN row 13.
    calls = []
    monkeypatch.setattr(app_module, "payment_gateway", lambda cost: calls.append(cost) or {"status": True, "txn_id": len(calls)})
    if sid == "18":
        client.get("/reservation/start?id=18")
    snapshot = db_path.read_bytes()
    assert client.get(f"/reservation/end?id={sid}&lat=1&lng=1").status_code == 200
    db_path.write_bytes(snapshot)                                     # the restore
    assert client.get(f"/reservation/end?id={sid}&lat=1&lng=1").status_code == 200
    cleared = [d["detail"]["reservation_id"] for r in read_records() for d in r["decisions"]
               if d["step"] == "reservation" and d["outcome"] == "cleared"]
    assert len(cleared) == 2 and cleared[0] == cleared[1]             # the same key, twice
    assert len(calls) == 2 and calls[0] == calls[1] > 0               # and the rider was charged both times



# ---- the write guard: a write never drops or invents a scooter ---------------------------------

def _rows_hook(monkeypatch, change):
    real = app_module.convert_db_to_dictlist
    monkeypatch.setattr(app_module, "convert_db_to_dictlist", lambda db: change(real(db)))


@pytest.mark.parametrize("change,problem", [
    (lambda rows: [r for r in rows if r["id"] != "15"],
     "the write would remove 1 and add 0 scooter(s) (removed ids ['15'], added ids [])"),
    (lambda rows: rows + [{"id": "99", "lat": 0, "lng": 0, "is_reserved": False, "reservation": None}],
     "the write would remove 0 and add 1 scooter(s) (removed ids [], added ids ['99'])"),
    (lambda rows: [dict(r, id="15b") if r["id"] == "15" else r for r in rows],
     "the write would remove 1 and add 1 scooter(s) (removed ids ['15'], added ids ['15b'])"),
])
@pytest.mark.parametrize("url", ["/reservation/start?id=18", "/reservation/end?id=33&lat=1&lng=1"])
def test_a_write_that_changes_the_set_of_scooters_is_refused(client, monkeypatch, db_path, read_records, change, problem, url):
    _rows_hook(monkeypatch, change)
    before = db_path.read_bytes()
    assert_envelope(client.get(url), FailureKind.INTERNAL_ERROR)
    assert db_path.read_bytes() == before                              # scooter 15 is still there
    r = read_records()[-1]
    assert r["db_write"]["error"] == "InvalidDbState"
    assert [d["detail"]["error"] for d in r["decisions"] if d["step"] == "db_write"] == [problem]


def test_a_write_of_a_rebuilt_scooter_list_is_refused(client, monkeypatch, db_path):
    # e.g. a bug that filters the loaded list into a new plain list: the guard can no longer
    # tell what was loaded, so it refuses rather than trusting it.
    real = app_module.write_db
    monkeypatch.setattr(app_module, "write_db", lambda db, path=None: real([s for s in db if s.id != "15"], path))
    before = db_path.read_bytes()
    assert_envelope(client.get("/reservation/start?id=18"), FailureKind.INTERNAL_ERROR)
    assert db_path.read_bytes() == before


def test_init_db_records_the_loaded_ids(db_path):
    db = app_module.init_db(str(db_path))
    assert db.loaded_ids == {"15", "18", "9", "14", "33"} == {s.id for s in db}
