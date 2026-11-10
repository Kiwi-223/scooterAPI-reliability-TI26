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
    elif step[0] == "snapshot":
        shutil.copyfile(db, db + ".snap")
    elif step[0] == "restore":
        shutil.copyfile(db + ".snap", db)
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
]


def _run(tmp_path, app_source, steps, db_path, records_path):
    planted = tmp_path / "app_copy"
    planted.mkdir(exist_ok=True)
    (planted / "app.py").write_text(app_source)
    for name in ("records.py", "failures.py", "payments.py"):
        (planted / name).write_text((REPO / name).read_text())
    env = dict(os.environ, ENABLE_FAULT_INJECTION="1", PAYMENT_TIMEOUT_DELAY_MS="0", PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run([sys.executable, "-c", _SCENARIO, str(planted), json.dumps(steps)], env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _cli_anomalies(records_path, capsys):
    code = rq.main(["--file", str(records_path), "--json", "anomalies"])
    found = json.loads(capsys.readouterr().out)
    return code, sorted({a["type"] for a in found})


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
