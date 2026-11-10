"""The cold agent's evidence tool sees the payment bugs a grader would plant (PLAN PR 14b).

PR #17's reviewer planted 8 bugs in the end-ride state machine. The test suite caught them
all, but `make records ARGS="anomalies"` (what an agent without the code runs) missed some.
Each test here plants one bug in a copy of app.py, drives a fault scenario through it in a
fresh interpreter, and checks that `anomalies` names the bug. The same scenario on the
unmodified app gives no anomalies, so the rule isn't firing on legitimate flows.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import records_query as rq

REPO = Path(__file__).resolve().parent.parent

_SCENARIO = """
import json, os, shutil, sys
sys.path.insert(0, sys.argv[1])
import app
client = app.app.test_client()
db = os.environ["SCOOTER_DB_PATH"]
statuses = []
for step in json.loads(sys.argv[2]):
    if step[0] == "start":
        statuses.append(client.get(f"/reservation/start?id={step[1]}").status_code)
    elif step[0] == "end":
        _, sid, lat, lng, fault = step
        resp = client.get(f"/reservation/end?id={sid}&lat={lat}&lng={lng}", headers={"X-Payment-Fault": fault})
        statuses.append(resp.status_code)
    elif step[0] == "snapshot":                  # ["snapshot"] or ["snapshot", name]
        shutil.copyfile(db, db + ".snap" + "".join(step[1:]))
    elif step[0] == "restore":
        shutil.copyfile(db + ".snap" + "".join(step[1:]), db)
    elif step[0] == "gateway_before_15b":        # a decline isn't replayed (PLAN PR 15b's bug)
        import payments
        payments.REPLAY_DECLINES = False
    elif step[0] == "gateway_15b":
        import payments
        payments.REPLAY_DECLINES = True
    elif step[0] == "reconcile":                 # ["reconcile", fault, max_attempts]
        import reconcile
        os.environ["PAYMENT_FAULT"] = step[1]
        reconcile.run(max_attempts=step[2])
        os.environ["PAYMENT_FAULT"] = "none"
print(json.dumps(statuses))
"""

DECLINE = [["start", "18"], ["end", "18", 1, 1, "decline"]]
RETRY_ELSEWHERE = [["start", "18"], ["end", "18", 1, 1, "error"], ["end", "18", 5, 5, "none"]]

# (id, what the bug does, source to replace, replacement, scenario, the anomaly that must name it)
BUGS = [
    ("M1", "decline records no balance",
     "\t\t\t\t\tif outcome == 'decline':\n\t\t\t\t\t\trecord_outstanding_balance(scooter)\n", "",
     DECLINE, "decline_not_settled"),
    ("M2", "timeout_not_charged ends the ride as paid",
     "\t\t\t\tif payment_response.ok or outcome == 'decline':",
     "\t\t\t\tif payment_response.ok or outcome in ('decline', 'timeout_not_charged'):",
     [["start", "18"], ["end", "18", 1, 1, "timeout_not_charged"]], "ended_without_payment"),
    ("M3", "phase-1 (payment_pending) write removed",
     "\t\t\t\t\tbegin_payment(scooter, end_lat, end_lng)\n\t\t\t\t\twrite_db(db, txn.path)\n",
     "\t\t\t\t\tbegin_payment(scooter, end_lat, end_lng)\n",
     [["start", "18"], ["end", "18", 1, 1, "none"]], "charged_before_pending_persisted"),
    ("M4", "a retry re-prices at the request's coordinates",
     "\t\t\t\t\treservation = scooter.reservation\n\t\t\t\t\trecords.note('end', 'retry'",
     "\t\t\t\t\tbegin_payment(scooter, end_lat, end_lng)\n\t\t\t\t\treservation = scooter.reservation\n"
     "\t\t\t\t\trecords.note('end', 'retry'",
     RETRY_ELSEWHERE, "pending_location_changed"),
    ("M5", "the ride ends at the request's coordinates",
     "\t\t\t\t\tfinish_ride(scooter)\n\t\t\t\t\twrite_db(db, txn.path)\n",
     "\t\t\t\t\tfinish_ride(scooter)\n\t\t\t\t\tscooter.lat, scooter.lng = end_lat, end_lng\n\t\t\t\t\twrite_db(db, txn.path)\n",
     RETRY_ELSEWHERE, "end_location_not_persisted"),
    ("M6", "decline keeps the ride pending",
     "\t\t\t\tif payment_response.ok or outcome == 'decline':", "\t\t\t\tif payment_response.ok:",
     DECLINE, "decline_not_settled"),
    ("M7", "conflict ends the ride (a free ride)",
     "\t\t\t\tif payment_response.ok or outcome == 'decline':",
     "\t\t\t\tif payment_response.ok or outcome in ('decline', 'conflict'):",
     [["start", "18"], ["snapshot"], ["end", "18", 1, 1, "none"], ["restore"], ["end", "18", 2, 2, "none"]],
     "ended_without_payment"),
    ("M8", "the decline's balance is recorded as 0",
     "\t\t\t\t'amount': reservation['amount'],\n", "\t\t\t\t'amount': 0,\n",
     DECLINE, "decline_not_settled"),
    # PR #18's review: the failure class that matters most, a double charge
    ("M9", "a new idempotency key per attempt: a charged timeout + retry charges twice",
     "\tpayment_response = payments.charge(reservation['idempotency_key'], scooter.id,",
     "\tpayment_response = payments.charge(reservation['idempotency_key'] + '-' + str(reservation['payment_attempts']), scooter.id,",
     [["start", "18"], ["end", "18", 1, 1, "timeout_charged"], ["end", "18", 1, 1, "none"]], "double_charge"),
    ("M10", "the decline's balance is recorded under the wrong idempotency key",
     "\t\t\t\t'idempotency_key': reservation['idempotency_key'],\n", "\t\t\t\t'idempotency_key': 'wrong-key',\n",
     DECLINE, "decline_not_settled"),
]


def _run(tmp_path, app_source, steps, db_path, records_path, planted_file="app.py"):
    planted = tmp_path / "app_copy"
    planted.mkdir(exist_ok=True)
    for name in ("app.py", "records.py", "failures.py", "payments.py", "reconcile.py"):
        (planted / name).write_text((REPO / name).read_text())
    (planted / planted_file).write_text(app_source)
    env = dict(os.environ, ENABLE_FAULT_INJECTION="1", PAYMENT_TIMEOUT_DELAY_MS="0", PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run([sys.executable, "-c", _SCENARIO, str(planted), json.dumps(steps)], env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _cli_anomalies(records_path, capsys):
    """(exit code, unresolved finding types); `--db` defaults to $SCOOTER_DB_PATH, the scenario's DB."""
    code = rq.main(["--file", str(records_path), "--json", "anomalies"])
    found = json.loads(capsys.readouterr().out)
    return code, sorted({a["type"] for a in found if not a.get("resolved")})


@pytest.mark.parametrize("bug,what,old,new,steps,rule", BUGS, ids=[b[0] for b in BUGS])
def test_anomalies_name_each_planted_payment_bug(tmp_path, db_path, records_path, capsys, bug, what, old, new, steps, rule):
    source = (REPO / "app.py").read_text()
    assert source.count(old) == 1, f"{bug}: app.py changed shape; update this test"
    _run(tmp_path, source.replace(old, new), steps, db_path, records_path)
    code, types = _cli_anomalies(records_path, capsys)
    assert code == 1 and rule in types, (bug, what, types)


@pytest.mark.parametrize("steps", sorted({json.dumps(b[4]) for b in BUGS}))
def test_the_same_scenarios_are_clean_on_the_real_app(tmp_path, db_path, records_path, capsys, steps):
    _run(tmp_path, (REPO / "app.py").read_text(), json.loads(steps), db_path, records_path)
    assert _cli_anomalies(records_path, capsys) == (0, [])


# ---- planted in reconcile.py (PR #20 review): bugs only the suite saw ---------------------------

# a declined ride, restored and ended again the same way; then a later snapshot brings its balance back
REPLAY = [["start", "18"], ["snapshot", "a"], ["end", "18", 1, 1, "decline"], ["snapshot", "b"], ["restore", "a"]]
# ... built with the gateway before PR 15b (charged on the replay), then reconciled
CHARGED_AND_OWED = REPLAY + [["gateway_before_15b"], ["end", "18", 1, 1, "none"], ["gateway_15b"], ["restore", "b"],
                             ["reconcile", "none", 3]]

RECONCILE_BUGS = [
    ("R1", "settle_charged pays under a new key: the ride ends charged AND owing a balance",
     "        payment = app_module.pay(scooter)\n",
     "        reservation['idempotency_key'] = reservation['idempotency_key'] + '-r'\n"
     "        payment = app_module.pay(scooter)\n",
     [["start", "18"], ["end", "18", 1, 1, "timeout_charged"],
      ["reconcile", "error", 3], ["reconcile", "error", 3], ["reconcile", "error", 3]],
     # since PLAN PR 15b the same run's clear_paid_balance voids that balance (charged_and_owed
     # is then historical, resolved); the key the planted code charged under still names it
     "payment_key_mismatch"),
    ("R2", "a restored ride is settled at the scooter's current position, not the ledger's",
     '"end_lat": decision["end_lat"],\n                                "end_lng": decision["end_lng"],',
     '"end_lat": scooter.lat,\n                                "end_lng": scooter.lng,',
     [["start", "18"], ["snapshot"], ["end", "18", 3, 3, "none"], ["restore"], ["reconcile", "none", 3]],
     "end_location_not_persisted"),
    # PLAN PR 15b
    ("R3", "clear_paid_balance leaves the paid balance open",
     "                if app_module.is_open_balance(balance) and balance[\"reservation_id\"] in by_reservation:\n",
     "                if False:\n",
     CHARGED_AND_OWED, "reconcile_not_settled"),
    ("R4", "settle_declined_from_ledger ends the restored declined ride without a balance",
     "\"decline\"\n                app_module.record_outstanding_balance(scooter)\n", "\"decline\"\n",
     [["start", "18"], ["snapshot"], ["end", "18", 1, 1, "decline"], ["restore"], ["end", "18", 2, 2, "none"],
      ["reconcile", "none", 3]], "reconcile_not_settled"),
    ("R5", "reconcile never finds a paid balance: the rider still owes in the DB",
     "        if charge is not None:\n            found.append((balance, charge))\n", "",
     CHARGED_AND_OWED, "charged_and_owed"),
]


# ---- planted in payments.py (PLAN PR 15b) ---------------------------------------------------

REPLAYED_AND_RESTORED = REPLAY + [["end", "18", 1, 1, "none"], ["restore", "b"]]


def test_anomalies_name_a_gateway_that_charges_a_replayed_decline(tmp_path, db_path, records_path, capsys):
    source = (REPO / "payments.py").read_text()
    old = "REPLAY_DECLINES = True\n"
    assert source.count(old) == 1
    _run(tmp_path, source.replace(old, "REPLAY_DECLINES = False\n"), REPLAYED_AND_RESTORED, db_path, records_path,
         planted_file="payments.py")
    assert _cli_anomalies(records_path, capsys) == (1, ["charged_and_owed"])


def test_the_real_gateway_declines_the_replay_again(tmp_path, db_path, records_path, capsys):
    statuses = _run(tmp_path, (REPO / "payments.py").read_text(), REPLAYED_AND_RESTORED, db_path, records_path,
                    planted_file="payments.py")
    assert statuses == [200, 422, 422]          # start, decline, the replay declined again (no charge)
    assert _cli_anomalies(records_path, capsys) == (0, [])


@pytest.mark.parametrize("bug,what,old,new,steps,rule", RECONCILE_BUGS, ids=[b[0] for b in RECONCILE_BUGS])
def test_anomalies_name_each_planted_reconcile_bug(tmp_path, db_path, records_path, capsys, bug, what, old, new, steps, rule):
    source = (REPO / "reconcile.py").read_text()
    assert source.count(old) == 1, f"{bug}: reconcile.py changed shape; update this test"
    _run(tmp_path, source.replace(old, new), steps, db_path, records_path, planted_file="reconcile.py")
    code, types = _cli_anomalies(records_path, capsys)
    assert code == 1 and rule in types, (bug, what, types)


@pytest.mark.parametrize("steps", [json.dumps(b[4]) for b in RECONCILE_BUGS])
def test_the_reconcile_scenarios_are_clean_on_the_real_code(tmp_path, db_path, records_path, capsys, steps):
    _run(tmp_path, (REPO / "reconcile.py").read_text(), json.loads(steps), db_path, records_path,
         planted_file="reconcile.py")
    assert _cli_anomalies(records_path, capsys) == (0, [])
