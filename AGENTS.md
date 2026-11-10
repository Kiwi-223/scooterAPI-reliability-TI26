# AGENTS.md

Onboarding for anyone, human or AI agent, who opens this repo with no prior context.
Keep it accurate: every PR that changes setup, commands, layout or data locations updates
this file in the same PR.

## What this is

A small scooter-rental HTTP API written in 2019: Python + Flask, with a JSON file as the
database. Clients can list available scooters, search by location and radius, start a
reservation, and end it. Ending a reservation pays through a **simulated** payment
gateway (`payments.py`) with a persisted ledger and reproducible faults (see "Payments &
fault injection"). [README.md](README.md) is the API contract (endpoints, params, status codes).
Keep it backwards compatible.

The repo is being modernised and made debuggable by an agent with no context, following
[PLAN.md](PLAN.md), in one focused PR at a time. [ACTIVITY.md](ACTIVITY.md) has the goals
("Required outcomes"). Many sections below say **coming in PLAN PR n**. Those things don't
exist yet, so don't go looking for them.

## Debugging a symptom (start here)

You've been handed a one-line symptom ("I ended my ride on scooter 18 but it still shows
as reserved") and possibly a bug someone planted. The goal: **name the root cause, back it
with evidence the system recorded, and write a failing test.** Follow these steps in
order; every command is copy-pasteable from the repo root.

**0. Set up** (once): `make setup`. The MCP tools (step 4) need it too.

**1. Read the symptom.** Note the scooter id, the endpoint (`/reservation/end`,
`/view_all_available`, …) and what was expected vs seen.

**2. Look for a planted change first.** The bug may be uncommitted.
```sh
git status --short && git diff --stat     # uncommitted code changes (a planted bug often lives here)
git log --oneline -5                      # or a recent commit
```
Every request record carries `git_dirty` / `git_dirty_paths` too: which code files differed
from `HEAD` when that server started (see Request records). Note them, but **don't
conclude from reading the diff**: step 3 gets the evidence.

**3. Reproduce on an isolated instance.** A fresh checkout has no records, so make some:
```sh
make scenario                    # lists the scenarios
make scenario NAME=rides         # start/end rides and check /view_all_available (the ACTIVITY symptom)
make scenario NAME=all           # every scenario: rides, errors, payment-faults, retry-settle,
                                 # decline, concurrent, restore, reconcile
```
- Each run uses the **working-tree code** (a fresh interpreter with its own bytecode
  cache, so a stale `__pycache__` can't hide a planted bug) against its own DB, records
  and ledger in `data/scenarios/<name>/`. The live files are never touched. A previous run
  is moved to `data/scenarios/<name>.<timestamp>/`.
- Every step states what it expected. It prints **`UNEXPECTED`** for each step that
  didn't happen as specified, then the anomalies, the invariant violations, where the
  evidence is, and the exact commands to query it. It exits 1 if anything is off.
- Faults are forced per request, so a re-run makes the same requests with the same
  request ids (`scn-<name>-<n>`); only reservation ids (uuids) differ.
- If the symptom needs something no scenario does, make the requests by hand on a scratch
  server (Configuration, "Recipes" in Payments & fault injection), or look at the live
  records (`data/records.jsonl`) if the symptom happened there.

**4. Query the evidence.** MCP tools first (`scenario="<name>"` reads a scenario run;
omit it for the live files); the CLI is the fallback and needs no approval.

| Question | MCP tool | CLI |
|---|---|---|
| What failed? | `recent_failures`, `failure_counts` | `make records ARGS="--file data/scenarios/rides/records.jsonl recent-failures"` |
| What happened to scooter 18? | `scooter_history(scooter_id="18")` | `make records ARGS="--file data/scenarios/rides/records.jsonl scooter 18"` |
| Everything one request decided | `request_trace(request_id=…)` | `make records ARGS="--file data/scenarios/rides/records.jsonl request scn-rides-004"` |
| Which records contradict themselves? | `anomalies` | `make records ARGS="--file data/scenarios/rides/records.jsonl anomalies --db data/scenarios/rides/db.json"` |
| Payments vs records | `ledger_check`, `payment_lookup` | `make records ARGS="--file data/scenarios/rides/records.jsonl ledger-check --ledger data/scenarios/rides/ledger.jsonl --min-age 0"` |
| DB + ledger + records consistent? | `check_invariants` | `SCOOTER_DB_PATH=data/scenarios/rides/db.json SCOOTER_RECORDS_PATH=data/scenarios/rides/records.jsonl SCOOTER_PAYMENTS_LEDGER_PATH=data/scenarios/rides/ledger.jsonl .venv/bin/python tests/invariants.py` |
| What's in the DB now? | `scooter_state(scooter_id="18")` | `cat data/scenarios/rides/db.json` |

**5. Locate the bad code path** with the record's three snapshots and its decisions:
- `scooter_before`: what the request read. `scooter_after`: what is **on disk** after it.
  `scooter_in_memory`: what the code **believed** when it finished.
- `in_memory` right but `after` wrong, with `db_write: ok` → the code changed the scooter
  but didn't persist it (wrong order, a missing write, writing a copy): anomaly
  `write_not_persisted`.
- `in_memory` wrong too → the logic itself is wrong; read the `decisions` in order to see
  which step went the wrong way.
- `db_write: refused` with a problem, or an `exception` field → the code built an invalid
  state, or crashed; the message names the field.
- `params: ok` on input that should have been rejected → validation.
- `payment` decisions: `gateway_outcome`, `fault` (why that outcome) and
  `simulator.charged` (did money move) → payment-path bugs. Then grep for the decision
  that went wrong: each `records.note('<step>', '<outcome>', …)` call in `app.py` names it.

**6. Write a failing test** that reproduces it through the API and checks the persisted
state. The fixtures are in `tests/conftest.py` (see Test): `client`, `read_db`,
`read_records`, `read_ledger`; every test gets its own DB, records and ledger. Template
(`tests/test_<symptom>.py`):
```python
def test_ending_a_ride_frees_the_scooter(client, read_db, read_records):
    assert client.get("/reservation/start?id=18").status_code == 200
    resp = client.get("/reservation/end?id=18&lat=23.01&lng=19.01")
    assert resp.status_code == 200
    assert read_db()["18"]["is_reserved"] is False                        # what's on disk
    [record] = [r for r in read_records() if r["request_id"] == resp.headers["X-Request-ID"]]
    assert record["scooter_after"]["is_reserved"] is False                # what the system recorded
    assert "18" in [s["id"] for s in client.get("/view_all_available").get_json()]   # what the user sees
```
Find the record by the response's `X-Request-ID`: every request (the `/view_all_available`
check too) appends one, so "the last record" is rarely the one you mean.
Payment faults: `monkeypatch.setenv("PAYMENT_FAULT", "decline")` (or `timeout_charged`,
`timeout_not_charged`, `error`). Run it: `make test ARGS="tests/test_<symptom>.py"`; it
must **fail** on the buggy code, for the reason you found.

**7. Fix it and confirm** the test passes, then `make test` (everything) and
`make scenario NAME=all` (no `UNEXPECTED`, exit 0).

### Worked examples: "I ended my ride on scooter 18 but it still shows as reserved"

All three come from real runs. `tests/test_playbook.py` plants A and B (plus a payment
bug and a validation bug), runs the scenario and checks the evidence names each one.

**A. The end writes the DB before clearing the ride** (a persistence-order bug in
`end_reservation`: `write_db` moved above `finish_ride`).
```text
$ make scenario NAME=rides
scenario rides: 12 requests, 5 unexpected step(s), 9 anomaly(ies), 0 invariant violation(s), ledger-check clean
  UNEXPECTED step 5: after its ride ended, 18 is listed again (the ACTIVITY symptom): expected scooter 18 listed, but it is not  [GET /view_all_available, request scn-rides-005]
  ANOMALY write_not_persisted in request scn-rides-004
  ANOMALY end_left_reserved in request scn-rides-004
$ make records ARGS="--file data/scenarios/rides/records.jsonl scooter 18"
  … 200 ok  GET /reservation/end id=scn-rides-004 scooter=18
      before=reserved@(23.0,19.0) after=reserved@(23.0,19.0) in_memory=free@(23.01,19.01) payment=success
```
The end returned 200 and charged, the code believed the scooter was free
(`in_memory=free`), but what was written still has the ride (`after=reserved`):
`write_not_persisted`. `make records ARGS="--file … request scn-rides-004"` shows the
order: `… payment ok, db_write ok, reservation cleared`, i.e. the write happened
**before** the ride was cleared, and `git_dirty_paths: ["app.py"]` says where to look.
Root cause: in `end_reservation`, `write_db` runs before `finish_ride`. The test template
above fails on `read_db()["18"]["is_reserved"]`.

**B. `finish_ride` forgets `is_reserved = False`** (the write guard refuses the invalid
state).
```text
$ make scenario NAME=rides
  UNEXPECTED step 4: end the ride on 18 (paid): expected 200, got 500 internal_error  [... request scn-rides-004]
  UNEXPECTED step 5: after its ride ended, 18 is listed again (the ACTIVITY symptom): ...
$ make records ARGS="--file data/scenarios/rides/records.jsonl request scn-rides-004"
  … payment ok {"gateway_outcome": "success", …}   reservation cleared …
  db_write refused {"error": "entry 1: is_reserved is true but reservation is null"}
  "exception": {"type": "RuntimeError", "message": "refusing to write an invalid scooter database (…)"}
```
The ride was **charged** but the final write was refused, so the DB still says
`payment_pending` (anomaly `charged_without_persisted_end`). The refusal names the broken
invariant: `is_reserved` stayed `true` while the reservation was cleared. The test fails
with a 500.

**C. Not a code bug: the payment timed out.** On the live records:
```sh
make records ARGS="scooter 18"            # … 504 payment_timeout GET /reservation/end … payment=timeout_not_charged
make records ARGS="request <that id>"     # payment failed {"gateway_outcome": "timeout_not_charged", …}; reservation payment_pending
```
The ride is `payment_pending` by design (H3): the scooter stays reserved until a retry of
`/reservation/end` or `make reconcile` settles it (see Payment state machine, Reconcile).
`make scenario NAME=payment-faults` shows the same flow working as specified.

## Setup

Requires **Python 3.15** (pinned in `.python-version`) and `make`.

```sh
make setup        # creates .venv with python3.15, installs the locked runtime + dev deps
make setup PYTHON=/path/to/python3.15   # if python3.15 is not on your PATH
make help         # lists every target
```

- Dependencies are **fully locked with hashes**. `make setup` installs with
  `pip install --require-hashes -r requirements.txt -r requirements-dev.txt`.
- `requirements.in` holds the direct runtime deps: Flask, Werkzeug, geopy and gunicorn.
  `requirements-dev.in` holds the dev deps: pip-tools, pytest, jsonschema
  (`make check-settings`) and `mcp` (the agent tool server, see "Agent tools (MCP)"). `mcp`
  is a dev dependency on purpose: it's an agent/developer tool, not part of the deployed
  app (the Procfile runs only `app:app`), so the production install stays small.
- The `*.txt` files are generated by `pip-compile`. **Never edit them by hand.**
- **To add or change a dependency:** edit `requirements.in` or `requirements-dev.in`, run
  `make lock`, then `make setup`. Commit both the `.in` and the `.txt` files.
  `make lock UPGRADE=1` bumps every pin to the latest allowed version.
- `make setup` only reinstalls when a lock file changes (it uses a stamp file,
  `.venv/.installed`). It installs on top of the existing venv. To get a venv that exactly
  matches the lock, run `make clean && make setup`. `make clean` removes `.venv`. Agents
  can't run a raw `rm -rf`: it's a deny rule (see Agent boundaries).
- `.venv/` is git-ignored.

## Run

```sh
make run          # .venv/bin/python app.py -> http://localhost:8080 (Flask dev server)
curl -s localhost:8080/view_all_available
```

- Run from the repo root. The default DB path `scooter_db.json` is relative, and relative
  paths are resolved against the **current working directory**, not against `app.py`.

### Configuration (environment variables)

If a variable is unset or empty, the README default applies, and behaviour is exactly as
before.

| Variable | Default | Used by | Notes |
|---|---|---|---|
| `SCOOTER_RECORDS_PATH` | `data/records.jsonl` | `records.py`, once per request | Where request records are appended (see Request records). Relative to the cwd. The directory is created on demand. Read on every request, like `SCOOTER_DB_PATH`. |
| `SCOOTER_DB_LOCK_TIMEOUT` | `5` | `app.py`, per mutating request | Seconds to wait for the DB lock before answering `storage_busy` (503). Unparseable values fall back to 5; negative values mean 0 (don't wait). |
| `SCOOTER_PAYMENTS_LEDGER_PATH` | `data/payments_ledger.jsonl` | `payments.py`, per payment | The simulated gateway's ledger (see Payments & fault injection). Relative to the cwd; the directory is created on demand. |
| `PAYMENT_FAULT` | `none` | `payments.py`, per payment | `none` (or `success`), a fault to force on **every** attempt (`decline`, `timeout_not_charged`, `timeout_charged`, `error`), or `random` (a seeded draw). Any other value makes every end fail with `internal_error` (`PaymentConfigError` in the log), never a silent default. |
| `PAYMENT_SEED` | `0` | `payments.py`, `random` mode | Any string. Same seed + idempotency key + attempt → same outcome. |
| `PAYMENT_FAULT_RATE` | `0.5` | `payments.py`, `random` mode | Probability of a fault per attempt, in [0, 1]; anything else is a `PaymentConfigError`. |
| `RECONCILE_MAX_ATTEMPTS` | `3` | `reconcile.py` | H3's N: gateway attempts without a charge before `make reconcile` ends a ride with an outstanding balance. `--max-attempts` overrides it. |
| `RECONCILE_MAX_AGE_MINUTES` | `15` | `reconcile.py` | H3's T: minutes since payment started (`payment_requested_at`) before reconcile ends an uncharged ride with an outstanding balance. `--max-age-minutes` overrides it. |
| `PAYMENT_TIMEOUT_DELAY_MS` | `20` | `payments.py` | How long a simulated timeout waits. Keep it small: the DB lock is held during the call. |
| `ENABLE_FAULT_INJECTION` | unset | `payments.py`, per payment | Only the exact value `1` makes the server honour the `X-Payment-Fault` request header. **Never set it on a deployed server.** |
| `SCOOTER_DB_PATH` | `scooter_db.json` | `app.py` on every DB read/write, under both the dev server and gunicorn | Read on **every request**, so changing `os.environ` in a test takes effect without re-importing `app`. A relative path is resolved against the cwd. The file must already exist; the app doesn't create it. |
| `SCOOTER_SCENARIOS_DIR` | `data/scenarios` | `scenarios.py`, `mcp_server.py` | Where `make scenario` puts its runs, and where the MCP tools' `scenario=` looks. |
| `HOST` | `localhost` | `python app.py` / `make run` only | gunicorn ignores it; use `--bind`. |
| `PORT` | `8080` | `python app.py` / `make run` | Must be an integer. **gunicorn reads `PORT` too**: with no `--bind` it then listens on `0.0.0.0:$PORT` (see below). |

```sh
# Run against a scratch copy of the DB so the real scooter_db.json is never touched:
SCRATCH_DB=$(mktemp) && cp scooter_db.json "$SCRATCH_DB"
SCOOTER_DB_PATH="$SCRATCH_DB" SCOOTER_RECORDS_PATH="$(mktemp -d)/records.jsonl" PORT=8099 make run
```

`HOST` is a common name. zsh and bash don't export it by default, but some shells (e.g.
tcsh) and CI images do. If `make run` binds somewhere unexpected, check `env | grep HOST`.
- Production-style server: the Procfile runs plain `gunicorn app:app`. gunicorn binds
  `127.0.0.1:8000` by default, but if `PORT` is set in the environment it binds
  `0.0.0.0:$PORT`, which means every network interface. For local use, be explicit:
  `.venv/bin/gunicorn --no-control-socket --bind 127.0.0.1:8000 app:app`.
  Without `--no-control-socket`, gunicorn 26 creates a control socket at
  `~/.gunicorn/gunicorn.ctl`, outside the repo, and the Claude Code sandbox blocks that
  write (see Agent boundaries).
  - Keep gunicorn's `--timeout` (default 30 s) longer than `SCOOTER_DB_LOCK_TIMEOUT` plus
    the slowest payment call. Otherwise a worker killed mid-request returns gunicorn's HTML
    500 instead of our JSON (see Concurrency).
  - Deploy readiness is PLAN PR 19.
- Server logs: the Flask dev server prints request lines (`"GET /search?... 200"`) to
  **stderr**; only its banner goes to stdout. gunicorn logs **no** requests unless you run
  it with `--access-logfile -`, which sends them to stdout. Neither log is persisted.

### Stopping a background dev server (macOS)

On macOS the venv's interpreter shows in `ps` as
`.../Python.app/Contents/MacOS/Python app.py`, with a capital `P`. As a result,
`pkill -f "python app.py"` matches nothing and the server keeps holding the port, so the
next `make run` fails with "Address already in use".

Prefer running the server in the foreground and stopping it with Ctrl+C. If you have to
stop a background server, kill it by PID, but **check first that it's yours**. Other
agents or the user may be running their own servers on the same port, and the
first PID holding the port may not be the one you started.

```sh
lsof -nP -iTCP:8080 -sTCP:LISTEN     # which PID holds the port (use :8000 for gunicorn)
ps -o pid,lstart,command -p <pid>    # is it `... Python app.py` / gunicorn, started by you?
lsof -a -d cwd -p <pid>              # is its working directory this checkout?
kill <pid>                           # only if both checks say it's yours
```

Never run a blind `kill $(lsof -t ...)`.

## Test

```sh
make test                          # the single command: runs setup if needed, then pytest
make test ARGS="-k search -x"      # pass any pytest options through ARGS
```

The result should be all passed, with no `xfailed` (no known bug is pinned right now; see
"Known behaviour" below). CI runs the
same command (see CI below).

- **Layout:** tests live in `tests/`, and the pytest config is `pytest.ini`.
  - `tests/conftest.py` has the fixtures.
  - `tests/seed_db.json` is the test-owned seed data: 5 scooters, and `33` starts reserved.
    It's deliberately in the **legacy** shape (no `reservation` key; see "Database schema"),
    so every test also exercises legacy loading. `tests/test_reservation_state.py` covers
    the new shape.
  - `tests/messages.py` holds the exact `msg` strings the API returns today.
  - Test files: `test_view_all.py`, `test_search.py`, `test_reservation.py`,
    `test_failure_kinds.py`, `test_records.py`, `test_records_query.py`, `test_db_transactions.py`,
    `test_reservation_state.py`, `test_payments.py`, `test_end_ride.py`, `test_planted_payment_bugs.py`,
    `test_reconcile.py`, `test_restore_replay.py`, `test_rollback.py`, `test_mcp_server.py`,
    `test_playbook.py` (scenarios, planted-bug evidence, `make reset-local`),
    `test_known_bugs.py`, plus `test_settings_schema.py` and
    `test_settings_policy.py` for the agent policy.
  - `messages.err(msg, kind)` builds the expected error body.
  - `tests/invariants.py` checks the end-ride invariants against the DB, the ledger and the
    records (see "Payment state machine"). `test_end_ride.py` runs it after every fault ×
    scenario and after a seeded random soak. `.venv/bin/python tests/invariants.py` runs it
    read-only on the live files (see Reconcile, runbook step 5).
- **Isolation:** the `db_path` fixture is autouse. It copies `tests/seed_db.json` into
  each test's `tmp_path` and points `SCOOTER_DB_PATH` at that copy with
  `monkeypatch.setenv`. Every test therefore starts from the same data, whatever state the
  live `scooter_db.json` is in.
- **Guard:** the autouse `_guard_real_db` fixture fails a test if the live
  `scooter_db.json` changed while it ran. Never point `SCOOTER_DB_PATH` at the real file in
  a test.
  - **The guards also see changes made by anything else.** If another process writes the
    live `scooter_db.json`, `data/records.jsonl` or `data/payments_ledger.jsonl` while
    `make test` runs, whichever test
    happens to be running fails with one error, `… changed during this test`. Typical
    sources: a dev server running from the same checkout (`make run` plus `curl`, which
    rewrites the DB and appends records), a `git restore scooter_db.json` or
    `make reset-local`, or another
    agent working in this checkout.
  - It looks like a random flaky test, but it isn't one. **Don't run the server from this
    checkout while tests run.** Give the server a scratch DB and records path (see
    Configuration) or use another checkout. If you see that error once, check what else
    touched those files before calling it a flake.
- **Fixtures for new tests:**
  - `client`: a Flask test client. It sets `TESTING=True` for that test only, through
    `monkeypatch.setitem(app.app.config, "TESTING", True)`; a test can override it with
    `monkeypatch.setitem(app.app.config, "TESTING", False)`. Since PLAN PR 7, an unhandled
    exception does **not** propagate into the test, even with `TESTING=True`: the
    `Exception` catch-all turns it into a JSON `internal_error` 500. The traceback is in
    the log (`unhandled exception`), which pytest prints with a failing test; assert on it
    through `caplog`.
  - **Flask 3 gotcha (why PR 7's catch-all is registered for `Exception`):** in testing mode, Flask re-raises an
    unhandled exception **before** any handler registered for `500` /
    `InternalServerError` runs. A handler registered for `Exception` does still run.
    Verified on Flask 3.1.3:

    | Handler registered for | `TESTING=True` | `TESTING=False` |
    |---|---|---|
    | `500` | exception reaches the test | JSON 500 |
    | `Exception` | JSON 500 | JSON 500 |

    So a JSON `internal_error` handler must be registered for `Exception` (letting
    `HTTPException` through), or its tests must turn `TESTING` off.
  - `get_json(url, status)`: asserts the status code and a JSON content type, then returns
    the parsed body.
  - `read_db()`: returns what the app actually persisted, as `{id: scooter}`.
  - `records_path` (autouse) points `SCOOTER_RECORDS_PATH` at `<tmp_path>/records.jsonl`.
    `read_records()` returns the test's records as a list of dicts, and the autouse
    `_guard_real_records` fails a test that touches the live `data/records.jsonl`.
  - `ledger_path` (autouse) points `SCOOTER_PAYMENTS_LEDGER_PATH` at
    `<tmp_path>/payments_ledger.jsonl` and **clears every `PAYMENT_*` and
    `ENABLE_FAULT_INJECTION` variable**, so each test starts with an empty ledger and no
    faults whatever your shell exports. Set faults with `monkeypatch.setenv("PAYMENT_FAULT", …)`.
    `read_ledger()` returns the entries, and the autouse `_guard_real_ledger` fails a test
    that touches the live `data/payments_ledger.jsonl`.
- **Strictness:** `pytest.ini` sets `strict = true` and `filterwarnings = error`. Unknown
  markers and config keys are errors, an xfail that unexpectedly passes **fails** the run,
  and any warning fails the test, including a leaked file handle's `ResourceWarning`.
- **Known bugs are pinned as strict xfails** in `tests/test_known_bugs.py`. Each test
  asserts the *fixed* behaviour, names its PLAN PR in `reason`, and uses `raises=` to pin
  how it fails today. When a PR fixes a bug, its test XPASSes, which fails the run. That PR
  must then delete the `xfail` marker and tighten the assertion to the final contract.
  **Assert specifically, not just "no 500".** Check each precondition (for example, that
  start returned 200), that the code path you mean actually ran (for example, a spy on the
  gateway), and the persisted state. Otherwise an unrelated regression can make the test
  pass for the wrong reason (see AI_NOTES #8).
- **To reproduce a bug as a failing test:** add a normal test (not an xfail) that uses
  `client` or `get_json` plus `read_db`, and assert on both the response and the persisted
  state.

## CI

GitHub Actions runs `.github/workflows/test.yml` (workflow name **tests**, job **test**) on
every pull request and on every push to `master`.

- **What it does:** checks out the repo and installs the Python version in
  `.python-version` (3.15) with `actions/setup-python`, using `check-latest: true` so it
  gets the newest 3.15.x patch rather than the runner's cached one. It then runs
  `make test PYTHON=<that interpreter>`, which runs `make setup` (hash-locked install) and
  then pytest, and then `make check-settings`. **These are the same commands you run
  locally.** pip's download cache is keyed on `requirements*.txt`.
- **Settings:** `ubuntu-24.04`, `timeout-minutes: 10`, and `permissions: contents: read`.
  Checkout uses `persist-credentials: false`, so the job holds no write token.
- **Superseded runs:** a newer push to the same PR cancels the older run. Runs on `master`
  are never cancelled.
- **Action pinning:** actions are pinned to full commit SHAs, with the release named in a
  comment. To bump one, resolve the new tag with
  `gh api repos/actions/<name>/git/ref/tags/<tag>`. If `object.type` is `tag`,
  dereference it with `gh api repos/actions/<name>/git/tags/<sha>`. Then update both the
  SHA and the comment.
- **Checking results:**
  - `gh pr checks <pr> --repo Kiwi-223/scooterAPI-reliability-TI26` shows the status of a
    PR's checks.
  - `gh run list --repo Kiwi-223/scooterAPI-reliability-TI26 --workflow tests` lists runs.
  - `gh run view <id> --log-failed --repo Kiwi-223/scooterAPI-reliability-TI26` shows the
    logs of failed steps.
- **CI's patch version can change without a commit.** Because of `check-latest: true`, CI's
  Python patch version (3.15.x) moves when a new 3.15 patch is released, with no commit in
  this repo. That's intentional: CI tests on the latest stable 3.15. If CI breaks with no
  related change, compare the "Successfully set up CPython (3.15.x)" line in the run log with
  the last green run's.
- **Branch protection** that *requires* this check (the required-check name is **`test`**,
  the job name) before merging is a human decision (H2)
  and isn't set by agents.

## Where data lives

- **`scooter_db.json`** is the live database: a JSON list of scooters (see "Database
  schema" below). The app **rewrites it** whenever a reservation starts or ends, so any
  manual testing changes it. **Don't commit changes to it.**
- **To reset local state: `make reset-local`** (PLAN PR 17). It backs up, then resets
  **all** local state together: `scooter_db.json` (copied, then restored from git), the
  request records, the payments ledger and the scenario runs in `data/`, and the DB's
  `.lock` / leftover `.tmp` files, all moved into `local-backups/<UTC timestamp>/`
  (git-ignored). It deletes nothing, refuses while a request holds the DB lock, and
  `make reset-local DRY_RUN=1` shows what it would move. Stop servers that use these
  files first. **`git restore scooter_db.json` alone is not a reset:** the ledger and the
  records survive it, so it's a *rollback* (see Database schema, "What a rollback
  erases"): reconcile then rebuilds old debts from the ledger, and `rollback-check` lists
  every past ride as erased. The committed file is still in the legacy shape (no
  `reservation` key); the first write migrates it, which is one more reason not to commit
  it after a manual run.
- **`scooter_db.json.lock`** (i.e. `<SCOOTER_DB_PATH>.lock`) is the DB's lock file. It's
  empty and harmless, created on demand, and git-ignored (`*.lock`). A crashed write can
  leave a `.scooter_db.json.*.tmp` next to the DB, also git-ignored; the DB itself stays
  intact. See "Concurrency".
- **Request records:** `data/records.jsonl` (or `SCOOTER_RECORDS_PATH`), one JSON line
  per request: what was asked, every decision, the scooter before and after, the DB write,
  the failure kind and the code version. They're git-ignored and survive restarts. See
  "Request records". The server log (stderr for the dev server) has tracebacks and storage
  details on top of that.
- **Payment gateway ledger:** `data/payments_ledger.jsonl` (or
  `SCOOTER_PAYMENTS_LEDGER_PATH`), one JSON line per gateway call: key, attempt, outcome,
  whether money was taken, the `txn_id`, and why that outcome happened. It's git-ignored,
  shared by all workers and survives restarts. **`git restore scooter_db.json` doesn't
  reset it**; `make reset-local` does (above).
- **Scenario runs:** `data/scenarios/<name>/` (`make scenario`, see "Debugging a
  symptom"): each run's own DB, records and ledger, plus `steps.json` and
  `summary.json`. Earlier runs of a name are kept as `<name>.<timestamp>/`.
- **Backups:** `local-backups/<timestamp>/`, written by `make reset-local`; git-ignored.

## Database schema

`scooter_db.json` (or `SCOOTER_DB_PATH`) is a JSON list. Each entry:

```json
{"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": true,
 "reservation": {"reservation_id": "f36f087baa0842929afe8d9eaf0e8598",
                 "started_at": "2026-11-10T02:43:35.026Z",
                 "start_request_id": "manual-1",
                 "payment_state": "active",
                 "idempotency_key": "f36f087baa0842929afe8d9eaf0e8598"}}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Unique across the file; compared exactly (`"18 "` is a different id). |
| `lat`, `lng` | finite number | In [-90, 90] and [-180, 180]. |
| `is_reserved` | boolean | Kept for compatibility (the README's public shape). |
| `reservation` | object or `null` | The active reservation, or `null` when the scooter is free. **Invariant: `is_reserved == (reservation is not null)`.** |
| `reservation.reservation_id` | non-empty string | uuid4 hex, new for every start. Returned as `reservation_id` by `/reservation/start`. Unique across the file. |
| `reservation.started_at` | string or `null` | UTC ISO-8601 with milliseconds and `Z`, set by the start. `null` only for legacy reservations. |
| `reservation.start_request_id` | string or `null` | The `X-Request-ID` of the start request, i.e. its request record: `make records ARGS="request <id>"`. `null` only for legacy reservations. |
| `reservation.payment_state` | non-empty string | `"active"` (ride in progress) or `"payment_pending"` (the ride was asked to end; its payment hasn't settled). See "Payment state machine". A state this code doesn't know is scoped to that one scooter (below). |
| `reservation.end_lat`, `end_lng`, `distance_m`, `amount` | numbers | **Only while `payment_pending`** (and then required): where the ride ends and what it costs, fixed by the **first** end request. Retries and reconcile reuse them. |
| `reservation.payment_requested_at`, `payment_request_id` | string or `null` | Only while `payment_pending`: when, and by which request (`X-Request-ID`), payment started. |
| `reservation.payment_attempts`, `last_payment_outcome` | int ≥ 0, string or `null` | Only while `payment_pending`: failed gateway attempts so far, and the last one's outcome (reconcile's "3 attempts or 15 minutes" rule, H3). |
| `outstanding_balances` | list, optional | H3 debts: one entry per ride that ended **without being paid**: `reservation_id`, `idempotency_key`, `amount`, `end_lat`, `end_lng`, `ended_at`, `reason` (`payment_declined` for a decline, `payment_unresolved` when reconcile's terminal rule ended it). Written in the same atomic write that ends the ride. Absent until the first decline on that scooter. Not shown by `/view_all_available`. **Settled balances** (PLAN PR 15b): when the ledger shows the reservation was charged after all (a restore replayed a declined ride into a charge), reconcile's `clear_paid_balance` keeps the entry for the audit trail and adds `settled_by_txn_id` (the charge), `settled_at` and `settled_reason`, all three or none. A balance is **owed (open) only without `settled_by_txn_id`**. **Rebuilt balances** (PLAN PR 15c): a balance a DB rollback erased and reconcile re-created from the ledger has `reason: rebuilt_from_ledger`, `original_reason` (`payment_declined` or `payment_unresolved`) and `ended_at` = the ledger entry's `ts`. |
| `reservation.idempotency_key` | non-empty string | **Equal to `reservation_id`** (one reservation, one ride, one charge). It's the key PLAN PR 13 sends to the gateway. It's a separate field so a later PR can change how it's derived without changing the id. Unique across the file. |
| `reservation.legacy` | boolean, optional | `true` on a synthesised legacy reservation (below). |
| *anything else* | any | **Unknown fields are kept**: top-level and inside `reservation`, they're written back unchanged (round-trip tested). They're not shown by `/view_all_available`, which returns exactly `id`, `lat`, `lng`, `is_reserved`. They do appear in request records' scooter snapshots. |

- **Lifecycle:** `/reservation/start` creates the reservation inside the DB transaction,
  in the same write that sets `is_reserved`. `/reservation/end` first saves it as
  `payment_pending`, then charges, then ends it (`null`, scooter freed and moved) or keeps
  it pending; see "Payment state machine". A refused start or end leaves it unchanged.
  Each step is a decision in the request record: `reservation: created`,
  `reservation: cleared`, `reservation: payment_pending`, `reservation: payment_abandoned`.
- **Validation (`init_db` → `find_db_shape_problem`):** every read checks the whole file.
  Any problem is `storage_error` (500), and nothing is written. Problems: a missing
  required field; a wrong type; `lat`/`lng` not finite or out of range; a duplicate `id`;
  a duplicate `reservation_id` or `idempotency_key` across scooters (one key would cover
  two rides; legacy entries count with their `legacy-<id>`); a broken invariant; a
  `reservation` that isn't an object or `null`, or that lacks a field or has a wrong
  type; a `payment_pending` reservation without its payment fields, or an `active` one
  with them; a malformed `outstanding_balances`. The msg names the entry index and the field, never a value from the file, e.g.
  `(entry 4: is_reserved is true but reservation is null)` or
  `(entry 4: duplicate reservation.reservation_id (same as entry 1))`.
- **A `payment_state` this code doesn't know affects only that scooter** (e.g. after
  rolling back past a PLAN PR that added `payment_pending`):
  - The file still loads. The scooter counts as reserved, so `/view_all_available` and
    `/search` leave it out, and every other scooter keeps working normally.
  - `/reservation/start` and `/reservation/end` on **that** scooter are `storage_error`
    (500): `The reservation for scooter 33 is in a payment state this server version
    does not handle (reservation.payment_state is not one of ['active', 'payment_pending']).` Nothing is
    charged or written, and the record has a `reservation: unsupported` decision with the
    state. Guessing could charge a ride twice.
  - Writes for other scooters copy that reservation back exactly as read. The write guard
    refuses any write that changes it or creates a state this code doesn't know.
  - To recover: roll forward to code that knows the state, or fix that entry by hand.
- **The app refuses to write a DB that would fail that validation** (for example, a code
  bug that reserves without a reservation, or reuses another scooter's reservation id),
  or that changes a reservation in an unknown payment state (compared with a copy taken
  at load), or whose set of scooter ids differs from the set this transaction loaded (a
  write must never silently drop or add a scooter; `init_db` returns a `ScooterDb` list
  that carries `loaded_ids`, and a plain list is refused). `write_db` checks before
  writing; the request
  becomes `internal_error` (500), the file is left as it was, and the record shows
  `db_write: {attempted: true, ok: false, error: "InvalidDbState"}` plus a
  `db_write: refused` decision naming the problem. Without this, one buggy request would
  turn every later request into `storage_error`.
- **Legacy files (written before PLAN PR 12) still load.** An entry without a
  `reservation` key is legacy:
  - `is_reserved: false` → `reservation: null`.
  - `is_reserved: true` (the committed DB's scooter `33`) → a synthesised reservation
    `{"reservation_id": "legacy-<id>", "started_at": null, "start_request_id": null,
    "payment_state": "active", "idempotency_key": "legacy-<id>", "legacy": true}`.
    It's deterministic, so every read and retry sees the same id. Ending it works like
    any other end; the `reservation: cleared` decision shows `legacy: true`.
  - Reads never write. The **first start or end rewrites the whole file in the new
    shape**, including legacy reservations (kept, with `legacy: true`).
  - Files may mix legacy and new entries. A `reservation` key that's present is always
    checked against the invariant.
  - **Why deterministic, not a random id per load:** reads never write, so a random id
    would change on every load until a write persisted it. A ride whose end fails before
    writing (payment failure, PLAN PR 14's timeouts) would then retry under a new key, so
    the gateway couldn't recognise the retry and could charge twice. A deterministic key
    keeps retries recognisable. Randomising wouldn't fix the restore hazard below anyway,
    because uuid reservations replay too.
- **Restoring a DB snapshot replays its reservations, and their keys.** This applies to any
  restored file (`git restore scooter_db.json`, a backup, a rollback), not just legacy
  entries. Every reservation in it comes back with the same `reservation_id` and
  `idempotency_key`, legacy (`legacy-33` returns on every `git restore`) or uuid. Ending
  it again uses that key a second time.
  - **Since PLAN PR 13 the ledger decides** (see "Payments & fault injection"): ending it
    the same way again (same scooter, reservation, **end location** and amount) returns
    the **original** charge's `txn_id` and ends the ride, with no second charge. Ending it
    anywhere else, even at the same distance (so the same amount), is `payment_conflict`
    (409): nothing is charged and the ride stays open (`active` again). Never a free ride,
    never a double charge (`test_a_restored_reservation_is_not_charged_twice_or_ridden_free`).
  - **But the restore itself breaks invariant I1** ("charged, yet the ride is `active`")
    until someone ends that ride where it was charged; the record's `existing_charge` says
    where. `make reconcile` finds and settles these from the ledger (see Reconcile).
  - **A restored ride that was declined** (PLAN PR 15b) is declined again when it's ended
    the same way: the gateway replays the decline, never charges it, and the ride ends with
    a `payment_declined` balance again. Ending it anywhere else is `payment_conflict`, and
    `make reconcile` settles it from the ledger's decline (`settle_declined_from_ledger`).
    Before 15b the replay was **charged**, and a later restore that brought the balance
    back left the reservation **charged and owing**; reconcile's `clear_paid_balance`
    voids such a balance (see Reconcile and AI_NOTES #35).
- **What a rollback erases** (PLAN PR 15c). A rollback (`git restore scooter_db.json` on its
  own, which was the documented reset before PLAN PR 17's `make reset-local`; a backup; any
  older snapshot) puts back the **whole** DB as it was.
  The **ledger and the request records survive it**: they're separate, append-only files.
  Everything the DB learned after the snapshot is gone:

  | Erased | Recovered? | How |
  |---|---|---|
  | **Outstanding balances** of rides that started after the snapshot, when the ledger has a decline or uncharged attempts for them | **yes** | `make reconcile` → `rebuild_lost_balance`, from the ledger (`reason: rebuilt_from_ledger`) |
  | **Rides in progress** (started after the snapshot, not yet ended) | **no: free rides** | nothing in the ledger knows where they ended or what they cost; `make records ARGS="rollback-check"` lists them |
  | A balance whose ride **never reached the gateway** (reconcile's `end_unpaid` by age, 0 attempts) | no | records only; `rollback-check` lists it as an erased balance |
  | Charged rides | nothing to recover | the ledger's charge is the ride's settlement (I2 holds) |
  | Rides that were in progress **at** the snapshot and ended since | **replayed**, not erased | they come back `active`/`payment_pending` with their keys: see the bullet above and Reconcile |
  | Settled markers (15b) and rebuilt balances (15c) | yes | `make reconcile` redoes them |
  | **Migrated state:** the legacy shape (`git restore` puts back scooter `33`'s `legacy-33` ride, `active`) and any field or `payment_state` a later PLAN PR added | yes / scoped | the first write migrates again; a ride replayed from it settles as above; an unknown `payment_state` is scoped to its scooter (above) |

  - **Rebuild guards** (`records_query.lost_balances`, used by reconcile): a balance is
    rebuilt only when the ledger shows the reservation ended unpaid (a decline, or only
    uncharged attempts: `error` / `timeout_not_charged`), **no** charge exists for it (by
    `reservation_id` or key: a charge always wins), and **no** scooter in the DB has a
    trace of it (no `active`/`payment_pending` reservation, no balance, open or settled,
    by `reservation_id` or key). It's re-created on the ledger's scooter, from the first
    decline (a replayed decline is the same ride) or else the latest uncharged attempt (the
    ride's latest stored end). Idempotent: once rebuilt, the DB has a trace. The scooter
    isn't moved: it may have been ridden since, so its position is left as restored. If
    the ledger's scooter isn't in the DB any more, there's nowhere to rebuild it: reconcile
    exits 1 (`lost_balances_open`) and `rollback-check` lists it.
  - **`payment_unresolved` balances** (reconcile's `end_unpaid`) are rebuilt the same way
    when the ledger has their uncharged attempts, which `end_unpaid` by attempts always
    has. A ride left `payment_pending` and erased is rebuilt too: nobody can retry a ride
    the DB no longer has, so H3's terminal rule applies at once. Only an `end_unpaid` **by
    age with 0 gateway attempts** has no ledger evidence: `rollback-check` lists it under
    "erased balances" (amount from the record), and a human decides.
  - **An I2 hit right after a restore** ("ended without a charge or an outstanding
    balance") usually means one of the rows above, not a code bug. Runbook (Reconcile,
    step 5): `rollback-check`, `make reconcile`, `rollback-check` again. What remains is
    the unrecoverable list (free rides, balances with no ledger trace), and I2's hits are
    exactly those reservations. Escalate them; don't edit the DB by hand.
- **Request records** show the same full form (`reservation` and unknown fields) in
  `scooter_before`, `scooter_after` and `scooter_in_memory`. A legacy reserved scooter's
  `scooter_before` shows its synthesised reservation, i.e. the scooter as the app loaded
  it, not the file's raw bytes.
- **Code:** `app.py`: `Scooter` (`to_dict` is the persisted form, `to_public_dict` the
  README form), `find_db_shape_problem`, `find_reservation_problem`, `load_reservation`,
  `legacy_reservation`, `new_reservation`, `is_supported_reservation`,
  `refuse_unsupported_reservation`, `find_balances_problem`, and the guard in `write_db`.

## Payment state machine

`/reservation/end` is **all or nothing** (PLAN PR 14, ACTIVITY Outcome 3). A ride's
reservation moves through these states, and every arrow is one atomic DB write:

```
            start                   end (phase 1: saved BEFORE any charge)
  free ──────────────► active ──────────────────────────────► payment_pending
   ▲                     ▲                                       │  charge (≤ 1 per key)
   │                     │ conflict: 409, nothing charged         │
   │                     └────────────────────────────────────────┤
   │  success / already_charged: 200 + txn_id                     │ timeout_* : 504, error: 502
   ├─────────────────────────────────────────────────────────────┤  (stays payment_pending;
   │  decline: 422, ride_ended + outstanding_balance (H3, H7)      │   a retry repeats the charge
   └─────────────────────────────────────────────────────────────┘   step with the SAME key)
```

| From | Event | To | Response | Money |
|---|---|---|---|---|
| `active` | end (phase 1) | `payment_pending` with `end_lat`/`end_lng`/`amount` | (continues) | none yet |
| `payment_pending` | `success` | free, at the stored end location | 200, `txn_id` | charged once |
| `payment_pending` | `already_charged` (an earlier attempt was charged, e.g. `timeout_charged`) | free, at the stored location | 200, that charge's `txn_id` | none new |
| `payment_pending` | `decline` | free, at the stored location, **+ an `outstanding_balances` entry** | 422 `payment_declined`, `ride_ended: true`, `outstanding_balance` | none |
| `payment_pending` | `timeout_not_charged` / `timeout_charged` | `payment_pending` (`payment_attempts`+1) | 504 `payment_timeout`, `ride_ended: false` | maybe (the ledger knows) |
| `payment_pending` | `error` (or an unreadable ledger) | `payment_pending` (`payment_attempts`+1) | 502 `payment_failed`, `ride_ended: false` | none |
| `payment_pending` | `conflict` (the key's charge is for another ride) | `active` (payment fields dropped) | 409 `payment_conflict`, `ride_ended: false` | none |
| `payment_pending` | start | unchanged | 422 `scooter_payment_pending` | none |

- **Retries:** `/reservation/end` on a `payment_pending` ride is a retry. It **ignores this
  request's `lat`/`lng`** (they must still be valid) and uses the stored end location and
  amount, so the gateway sees the same ride and charges it at most once. The record's
  `end: retry` decision shows both the stored and the requested coordinates.
- **If the final write fails after a charge** (disk full, a crash): the DB still says
  `payment_pending` with the key, so a retry (or reconcile) settles it with the existing
  charge. The record shows `payment: ok` followed by `db_write: failed`, and `anomalies`
  flags it as `charged_without_persisted_end`.
- **If the first write fails** (e.g. an unwritable DB): `storage_error`, and the gateway is
  **never called** (this was the PR 14 xfail).
- **Conflict, decided:** go back to `active` rather than stay pending. Staying pending would
  retry the same conflicting ride forever. Going back to `active` charges nothing, ends
  nothing, and lets the rider end it where the ledger says it was charged (which settles it
  as `already_charged`). Conflicts only come from restored DBs (PR 13); see Database schema.
- **The lock stays held across the gateway call, deliberately.** Correctness first: one
  exclusive DB lock around phase 1 → charge → phase 2 means no start, end or retry on any
  scooter can interleave with a half-finished payment, so there's nothing extra to prove.
  The alternative (unlock around the charge) would let a concurrent retry run the same
  key's charge and finalise in parallel; the ledger would still prevent a double charge,
  but both finalisers would race to write. The cost: a slow gateway delays every writer.
  Here it's bounded by `PAYMENT_TIMEOUT_DELAY_MS` (20 ms); a real gateway with multi-second
  timeouts would need the split (PLAN PR 19, deploy readiness).
- **Where things are stored:** the `txn_id` is in the ledger and in the end request's
  record (no ride history in the DB). An outstanding balance has to outlive the cleared
  reservation and be written atomically with the ride's end, so it lives in the DB, on the
  scooter entry, keyed by `reservation_id`. Nothing collects balances yet: there's no rider
  identity in this API.
- **Invariants** (`tests/invariants.py`, checked against the DB, the ledger and the
  records):
  - **I1** no charge without an ended or `payment_pending` ride, and no ride both charged
    **and** owing an open balance, checked per `reservation_id` (business identity), not
    only per idempotency key, so a bug that switches keys can't hide it (PR #20 review). A
    balance marked settled isn't owed, but its `settled_by_txn_id` must be a ledger charge
    for that same reservation (PLAN PR 15b);
  - **I2** no ended ride without exactly one charge or one outstanding balance (at most one
    open);
  - **I3** at most one charge per reservation, **whatever key it was charged under**
    (checked per idempotency key *and* per `reservation_id`, which every ledger entry
    carries), and no charge under a key that no reservation ever owned. A **double charge**
    across two keys (e.g. code that makes a new key per attempt) breaks this;
  - **I4** a reserved scooter always has an `active` or `payment_pending` reservation;
  - **I5** every charge's and every balance's **amount** equals the amount fixed when that
    ride's payment started (the records' `payment: requested` cost). I1–I4 check that a
    charge or balance *exists*; only I5 checks *how much*. (Added in PLAN PR 14b, after a
    review planted a 0-amount balance that I1–I4 accepted.)

  `test_end_ride.py` checks them after every step of every fault (`none`, `decline`, both
  timeouts, `error`) × {first attempt, retry, retry from a fresh process, 4 concurrent
  retries, final write failing}. It then checks that a healthy retry always settles the
  ride, paid or owed exactly once. A seeded soak (4 processes × 125 random starts and ends
  with `PAYMENT_FAULT=random`) is checked the same way, and `anomalies` must be clean. A
  **restored DB** can break I1 on its own (see Database schema); `make reconcile` settles
  that too. `tests/test_reconcile.py` checks I1–I5, `anomalies` and `ledger-check` after
  reconciling a random-fault soak (see Reconcile).

## Reconcile (stuck rides)

`make reconcile` (`reconcile.py`, PLAN PR 15) makes sure **no scooter stays reserved
forever** (ACTIVITY Outcome 3). It settles every `payment_pending` ride, every ride a
restored DB left `active` although the ledger shows it was paid or declined, and voids any
outstanding balance whose reservation the ledger shows charged (PLAN PR 15b). It's a command
to run or schedule. The server never runs it on its own.

```sh
make reconcile ARGS="--dry-run"        # what it would do; changes nothing, writes no records
make reconcile                         # do it (each ride: one transaction under the DB lock)
make reconcile ARGS="--json"           # machine-readable summary
make reconcile ARGS="--only 18 --max-attempts 3 --max-age-minutes 15"
.venv/bin/python -m reconcile --help
```

**What it decides**, per scooter. `reconcile.decide()` is a pure function of the scooter's
DB entry, the ledger and the time. The first matching row wins:

| State | Ledger | Action | Result |
|---|---|---|---|
| any (free too), with an **open outstanding balance** | a charge for that balance's reservation, on this scooter (a restore replayed a declined ride into a charge, PLAN PR 15b) | `clear_paid_balance` | marks every such balance settled (`settled_by_txn_id`, `settled_at`, `settled_reason`; kept, not deleted) and records `balance: settled`. A charge always wins: the rider paid, so they owe nothing. Nothing is charged |
| any (free too) | the ledger proves a debt (a decline, or only uncharged attempts) for a reservation of this scooter that has **no charge** and **no trace in the DB** (a rollback erased it, PLAN PR 15c) | `rebuild_lost_balance` | re-creates every such balance from the ledger (`reason: rebuilt_from_ledger`, `original_reason`), records `balance: rebuilt`; nothing is charged, the scooter isn't moved. Unusable ledger values are reported. See Database schema, "What a rollback erases" |
| `payment_pending` | a charge for **this** ride (same scooter, reservation, stored end location and amount), e.g. after `timeout_charged` | `settle_charged` | ends the ride with **that** `txn_id`; the gateway answers `already_charged`, so nothing new is charged |
| `payment_pending` | a charge under the key for a **different** ride | `report` | nothing changes; a human looks |
| `payment_pending` | no charge, a **decline** for this ride (only a restore gets a declined ride back to pending) | `settle_declined` | ends it with a `payment_declined` balance; **no gateway call** (it would only replay the decline) |
| `payment_pending` | no charge, a decline for a **different** ride | `report` | nothing changes |
| `payment_pending` | no charge, attempts < N **and** age < T | `retry` | one new gateway attempt (same key, stored location and amount): paid → ended; decline → ended with balance (H3); timeout/error → still pending (attempts +1) |
| `payment_pending` | no charge, attempts ≥ N **or** age ≥ T | `end_unpaid` | H3's terminal rule: ends the ride at the stored location with an outstanding balance (`reason: payment_unresolved`); **nothing is charged** |
| `active` | a charge for this reservation (a restored DB: invariant I1) | `settle_from_ledger` | ends the ride where the ledger says it ended, with the original `txn_id`; no new charge |
| `active` | a charge that doesn't fit (another scooter or reservation, or an amount that doesn't match the distance from where the scooter stands) | `report` | nothing changes |
| `active` | no charge, a **decline** for this reservation (a restored declined ride, ended elsewhere: `payment_conflict`) | `settle_declined_from_ledger` | ends the ride where the decline says it ended, with a `payment_declined` balance; no gateway call. A decline that doesn't fit is reported, as above |
| free (no open paid balance), or `active` with nothing in the ledger (riding) | — | `none` | nothing |

`report` is recorded as the failure kind **`reconcile_unsettled`** (409), with a msg naming
the scooter and the reason, so `make records ARGS="recent-failures"` and `failure-counts`
show it. Every settling decision records the end coordinates it settles at, which
`anomalies` (`end_location_not_persisted`) checks against `scooter_after`.

- **Passes.** A run makes up to 4 passes, so a scooter whose paid balance was cleared or
  whose lost balance was rebuilt gets its ride settled in the same run. A scooter that was retried or reported isn't touched
  again in that run. `--dry-run` shows the first pass only.
- **Why mark a paid balance settled instead of deleting it** (PLAN PR 15b): the balance
  was really recorded (the decline happened), and the charge that voids it came later.
  Keeping both, linked by `settled_by_txn_id`, lets a human see the whole story in the DB;
  invariant I1 checks that the txn is a real charge for that reservation.

- **The terminal rule (H3): N attempts or T minutes.**
  - N defaults to 3 (`--max-attempts`, `RECONCILE_MAX_ATTEMPTS`). The attempt count is the
    larger of the reservation's `payment_attempts` and the number of ledger entries for
    the key, so attempts by a worker that died mid-payment count too.
  - T defaults to 15 minutes (`--max-age-minutes`, `RECONCILE_MAX_AGE_MINUTES`), measured
    from `payment_requested_at`, i.e. when the first end request asked to pay.
  - **A charge always wins.** Before the terminal rule, the ledger is read under the DB
    lock. Every charge also happens under that lock, so no charge can slip in between. A
    ride paid by a timed-out attempt is never also given a balance.
  - Each run makes **one** attempt per ride, so attempts are spread over runs (e.g. a
    cron), not burned in a burst.
- **Settling a restored ride from the ledger** (rather than only reporting it): a
  reservation is one ride, and its key is unique. A charge for exactly this scooter and
  reservation proves the ride ended where it was charged, and the DB just forgot.
  Reconcile re-checks the amount against the distance from where the scooter stands, which
  is the ride's start after a restore, so a hand-edited or inconsistent entry is reported,
  not guessed at.
- **Every action is a request record** with path `/reconcile` and
  `X-Request-ID: reconcile-<run>-<n>` (the run id is printed). It carries a `reconcile`
  decision (the action and why), plus the same `payment`, `reservation`, `balance` and
  `db_write` decisions as a rider's request. So `make records ARGS="scooter <id>"`,
  `anomalies` and `ledger-check` all see what reconcile did. A dry run writes nothing.
- **Safety:**
  - **Concurrent with the server.** Each ride is decided again *inside* its own DB
    transaction, on fresh state, so a rider's request racing reconcile can't make it act
    on stale data (tested alongside live random-fault traffic).
  - **Idempotent.** A second run finds nothing to do, writes nothing and charges nothing
    (tested).
  - **Fails closed.** An unreadable ledger means nothing can be checked, so every
    reservation is reported and nothing changes. See "Unreadable ledger".
  - A `payment_state` this code doesn't know is reported (`storage_error`), not touched.
- **Exit codes:**
  - 0: no `payment_pending` ride left, no open balance the ledger shows paid
    (`paid_balances_open` in `--json`), no lost balance left to rebuild
    (`lost_balances_open`), and nothing reported.
  - 1: something remains (a ride still pending after this run's attempt, a reported
    problem, or a ledger it couldn't read afterwards); with `--dry-run`, there is something
    to do.
  - 2: usage error.

  (`make` turns non-zero into its own `Error`, so use `python -m reconcile` when the code
  matters.)
- **How it relates to `ledger-check`:** an open orphan (a request that died mid-payment)
  usually leaves a `payment_pending` ride whose key has a charge. Reconcile settles it as
  `settle_charged`, and the orphan then shows as resolved. Run `ledger-check` before and
  after if you're investigating a crash.
- **Runbook.**
  1. `make reconcile ARGS="--dry-run"` to see what would happen.
  2. `make reconcile`.
  3. If it exits 1, read its output. A ride `still_pending` just needs another run (or
     waits for N/T). A `report` needs a human: look at
     `make records ARGS="scooter <id>"` and `make payments ARGS="lookup <key>"`.
  4. Confirm with `make records ARGS="anomalies"` and `make records ARGS="ledger-check"`.
     `anomalies` exits 1 only for **unresolved** findings. A `charged_and_owed` labelled
     `[historical: resolved]` is history: its detail says `still_in_db: false` (the current
     DB holds no open balance for that reservation) and `settled_by` (the reconcile request
     that settled it, or null if a restore wiped it).
  5. **After a DB restore, confirm against the DB itself**, not only the records (the
     records can't see a restore): the DB-based invariants I1–I5 in `tests/invariants.py`.
     From a shell, with the server stopped or idle:
     ```sh
     .venv/bin/python tests/invariants.py     # read-only; exit 0 = all hold, 1 = one JSON line per violation
     ```
     It reads the same `SCOOTER_DB_PATH` / `SCOOTER_PAYMENTS_LEDGER_PATH` /
     `SCOOTER_RECORDS_PATH` as the app (a missing ledger or records file counts as empty).
     The current DB stands in for the "initial" one, so a ride from before the records
     began counts only while it's still in the DB. A
     `"I1 a reservation was charged and also owes a balance"` means a paid balance is still
     open: run `make reconcile`.
  6. **"What did the last restore erase?"** (PLAN PR 15c) An
     `"I2 ended without a charge or an outstanding balance"` after a restore is almost
     always the rollback, not a code bug (see Database schema, "What a rollback erases"):
     ```sh
     make records ARGS="rollback-check"         # lost balances, free rides, erased balances (exit 1 if any)
     make records ARGS="rollback-check --since 2h"   # only rides started in the last 2 hours
     make reconcile                             # rebuilds every lost balance the ledger proves
     make records ARGS="rollback-check"         # what's left can't be recovered automatically
     ```
     After reconcile, `lost balances` is 0. Each remaining **erased ride** is a free ride
     (started, then erased while in progress; no payment anywhere), and each **erased
     balance** a debt only the records remember. `tests/invariants.py`'s I2 hits are then
     exactly those reservations. Report them to a human with their reservation ids and
     start records; there's nothing to settle them with.
- **Scheduling (documented only; nothing here deploys it):** run it every minute or so next
  to the server, against the same data files. For example, a crontab line:
  `* * * * * cd /path/to/repo && .venv/bin/python -m reconcile >> data/reconcile.log 2>&1`.
  Runs are safe to overlap, because each ride is its own locked transaction.
- **Verified:**
  - **Tests** (`tests/test_reconcile.py`): every branch, both terminal rules with an
    injected clock, a restored ride, idempotency, dry run, and concurrency with live
    requests. `tests/test_restore_replay.py` (PLAN PR 15b): a declined ride replayed by a
    restore, with the balance still in the DB and with it wiped, both before and after
    reconcile; and 8 seeded restore-heavy random histories (300 steps each, per-scooter
    restores), with the pre-15b gateway and the current one, which all end with I1–I5
    holding, no unresolved anomaly and a clean `ledger-check` after reconcile.
    `tests/test_rollback.py` (PLAN PR 15c): each loss type under a whole-DB rollback, the
    rebuild guards, and 8 seeded whole-DB rollback histories (300 steps, reconcile also
    running mid-history) after which I1, I3–I5 hold, `anomalies` and `ledger-check` are
    clean, no lost balance is left, and I2's hits are exactly the reservations
    `rollback-check` reports as unrecoverable (11–31 free rides per history).
  - **Soak:** after a 500-operation multi-process random-fault soak, reconcile runs under
    the *same* random faults until nothing is pending. Then zero `payment_pending` rides
    remain, I1–I5 hold, `anomalies` is clean, `ledger-check` is clean, and another run has
    nothing to do.
  - **Live:** after gunicorn (4 workers) served 480 random-fault requests, two runs settled
    the 2 stuck rides. One was a charged timeout settled with its charge; the other was
    retried (a charged timeout again), then settled. Afterwards: 0 violations, 0 anomalies,
    and `ledger-check` clean.
- **Code:** `reconcile.py`: `decide` and `plan` (pure; the MCP server's `reconcile_plan` uses `run(dry_run=True)`), `run`,
  `main`. Each action runs through a tiny internal Flask app (never served) so it gets the
  main app's records and error handlers.

## Payments & fault injection

`/reservation/end` pays through `payments.charge()` (`payments.py`), a **simulated**
gateway. Nothing real is charged. It exists so payment failures can be produced and
replayed on demand (ACTIVITY Outcome 3).

**Outcomes** (each is a record's `payment` decision `gateway_outcome`, and a ledger entry's
`outcome`):

| Outcome | Money taken? | API answer | Ride |
|---|---|---|---|
| `success` | yes | 200 + `txn_id` (32 hex chars, a string) | ended |
| `already_charged` | no (found the key's earlier charge for the same ride: scooter, reservation, end location, amount) | 200 + **that** charge's `txn_id` | ended |
| `decline` | no | 422 `payment_declined`, `ride_ended: true`, `outstanding_balance` | ended, balance recorded (H3) |
| `timeout_not_charged` | no | 504 `payment_timeout`, `ride_ended: false` | `payment_pending` |
| `timeout_charged` | **yes** (the caller isn't told) | 504 `payment_timeout`, `ride_ended: false` | `payment_pending`; the next attempt is `already_charged` |
| `error` | no | 502 `payment_failed`, `ride_ended: false` | `payment_pending` |
| `conflict` | no (the key has a charge for a *different* ride) | 409 `payment_conflict`, `ride_ended: false` | `active` again |

See "Payment state machine" for what each one does to the ride.

**The ledger** (`data/payments_ledger.jsonl`, or `SCOOTER_PAYMENTS_LEDGER_PATH`) is the
gateway's memory: one JSON line per call, appended under an exclusive `flock` on the
ledger file. It's a different lock from the DB's. Every charge happens under the DB lock
too (the app and `reconcile` both charge inside a DB transaction), which is what makes
reconcile's "no charge yet" checks safe.
- **At most one charge per idempotency key.** Check-then-append happens under the lock, so
  workers and processes racing on one key produce exactly one charge (tested with 8
  processes).
- **Ride matching.** A key that already has a charge returns `already_charged` only if it's
  the **same ride**: scooter id, reservation id, **end location** (`end_lat`/`end_lng`,
  exactly, as the app parsed them, so `+23.009` equals `23.009`) and amount all match that
  charge. Otherwise `conflict`. The amount alone isn't enough: it's the rounded distance,
  so two end points ~2 km apart at the same distance would have looked like one ride
  (PR #16's review; AI_NOTES #27).
  `already_charged`/`conflict` win over any forced fault: the gateway knows it was paid.
- **A decline is replayed** (PLAN PR 15b, `payments.REPLAY_DECLINES`). A key with no charge
  but an earlier `decline` answers `decline` again for the same ride (nothing charged; the
  entry's `fault` is `{"source": "ledger", "replayed": "decline"}`), or `conflict` for a
  different ride. This also wins over any forced fault. Only a DB restore can ask again for
  a declined ride; charging it would leave the ride both charged and owing a balance.
  H3 calls a decline *definitive*, and a real gateway's idempotency replays the result.
- **Attempt numbers** are counted from the ledger (1 + earlier entries for the key), so
  they're the same whichever worker serves the retry.
- **Fails closed.** If any line can't be parsed, the gateway can't know whether a key was
  charged, so it answers `error` (502) without charging or appending, for **every** end,
  until the ledger is repaired. See "Unreadable ledger" below.
- Each entry: `ts`, `idempotency_key`, `attempt`, `outcome`, `charged`, `txn_id` (when
  charged), `scooter_id`, `reservation_id`, `end_lat`, `end_lng`, `amount`, `request_id` (the end request's
  `X-Request-ID`), `fault`, `existing_charge` (for `already_charged`/`conflict`), `pid`.

**Why a restored DB doesn't charge twice (and doesn't ride free).** A restored snapshot
replays reservations with their idempotency keys (see Database schema). A restored
reservation is the **same ride**: keys are per reservation, and nobody else can be riding
under it. So ending it **at the same place** returns the original charge: the rider paid
once, for that ride. Ending it anywhere else is a different ride, a conflict to surface,
not to guess at (`test_an_equidistant_end_after_a_restore_is_a_conflict_not_a_free_ride`).
This is PLAN row 13's rule: ride identity = scooter, reservation, end location (and
amount).

**Unreadable ledger (runbook).** Symptom: every end returns `502 payment_failed`, and the
records' payment `fault` is `{"source": "ledger", "reason": "ledger_unreadable",
"problem": "line N is …"}`. The usual cause is a process killed mid-append, which leaves a
partial last line.

```sh
make payments ARGS="verify"     # readable: exit 0. Not: exit 1 (make shows "Error 1"), the first
                                # bad line as JSON on stdout, and on stderr whether it's an
                                # interrupted append
```

1. **Stop every server** that uses this ledger, so nothing appends while you repair it.
2. **Back it up first:** `cp data/payments_ledger.jsonl data/payments_ledger.jsonl.bak-$(date +%s)`.
3. If `verify` says `"interrupted_append": true` (the bad line is the **last** line and
   has **no** trailing newline): that call died before it returned, so the app never got
   an answer and didn't end the ride. Remove only that line:
   `head -n <line-1> data/payments_ledger.jsonl > data/payments_ledger.jsonl.fixed && mv data/payments_ledger.jsonl.fixed data/payments_ledger.jsonl`
   (`<line-1>` is one less than the reported line). Run `verify` again and expect exit 0.
   A retry of that ride is then a normal new attempt.
   **This is safe even if the partial line shows `"charged": true`.** The append never
   completed, so that charge call never returned and no ride was ended on it. Because
   the gateway fails closed, nothing after it was charged either. Step 5's rule is about
   *complete* lines.
4. If it's **anything else** (a bad line in the middle, or a complete but malformed line):
   don't edit by hand. Keep the backup and escalate to a human. Charges after that line
   may depend on it.
5. **Never** delete, empty or replace the ledger with an older copy, and never edit or
   remove a line with `"charged": true`. The ledger is the only record of which keys were
   charged. Without it, every restored or retried ride would be charged again.

**Choosing faults**, in order of precedence:
1. The ledger (`already_charged` / `conflict`, or a replayed `decline`), as above.
2. The `X-Payment-Fault` request header, **only if `ENABLE_FAULT_INJECTION=1`** in the
   server's environment. Values: `none`/`success`, a fault name, or `random`.
   - Without the flag, the header is ignored. The record says so:
     `payment_fault_override: ignored`. An unknown value is `rejected`, and the env
     applies.
3. `PAYMENT_FAULT`: `none` (the default), a fault forced on every attempt, or `random`.
   `random` draws from `sha256(PAYMENT_SEED, idempotency key, attempt)`. A fault happens
   with probability `PAYMENT_FAULT_RATE`, and the fault itself is uniform over the four.
   Same inputs, same outcome, whichever worker or order; a retry (the next attempt) is a
   fresh, independent draw.

Every payment decision records `fault`: `{"source": "env", "mode": "decline"}`,
`{"source": "header", "mode": …}`, `{"source": "ledger"}`, or for a draw
`{"source": "env:random", "seed", "idempotency_key", "attempt", "rate", "draw"}`. That's
everything needed to replay it.

**Recipes.** Run a scratch server first, so the real DB, records and ledger aren't
touched. Run the curls and queries from another terminal, with the same `S` and exports.

```sh
S=$(mktemp -d) && cp scooter_db.json "$S/db.json"
export SCOOTER_DB_PATH="$S/db.json" SCOOTER_RECORDS_PATH="$S/records.jsonl" \
       SCOOTER_PAYMENTS_LEDGER_PATH="$S/ledger.jsonl" PORT=8099
ENABLE_FAULT_INJECTION=1 make run                  # header overrides allowed (scratch only!)

# Trigger each failure on demand (scooter 33 starts reserved; or start one first):
curl -s -H 'X-Payment-Fault: timeout_not_charged' 'localhost:8099/reservation/end?id=33&lat=24.5&lng=-28'  # 504, pending
curl -s -H 'X-Payment-Fault: error'               'localhost:8099/reservation/end?id=33&lat=0&lng=0'       # 502 (retry: stored location)
curl -s                                           'localhost:8099/reservation/start?id=33'                 # 422 scooter_payment_pending
curl -s -H 'X-Payment-Fault: timeout_charged'     'localhost:8099/reservation/end?id=33&lat=24.5&lng=-28'  # 504, charged, pending
curl -s                                           'localhost:8099/reservation/end?id=33&lat=24.5&lng=-28'  # 200, that charge's txn_id
curl -s                                           'localhost:8099/reservation/start?id=18'                 # 200
curl -s -H 'X-Payment-Fault: decline'             'localhost:8099/reservation/end?id=18&lat=23.5&lng=19'   # 422, ride ended + balance

# Or without the header: force one fault for every attempt, or a seeded mix
PAYMENT_FAULT=timeout_charged make run
PAYMENT_FAULT=random PAYMENT_SEED=42 PAYMENT_FAULT_RATE=0.7 make run

# What happened, and why:
make records ARGS="--file $S/records.jsonl scooter 33"     # each request's payment=<outcome>
make payments ARGS="lookup legacy-33"                      # the ledger entries for a key (uses the export)
make payments ARGS="draw --seed 42 --key legacy-33 --attempt 1 --rate 0.7"   # replay one draw
make payments ARGS="verify"                                # is the ledger readable?
```

- **Replaying a seeded failure exactly:** take `seed`, `idempotency_key`, `attempt` and
  `rate` from the record's `fault`. `make payments ARGS="draw …"` recomputes the outcome.
  To re-run the whole sequence, restore the same DB, give the server an **empty** ledger
  (attempt numbering restarts at 1) and the same `PAYMENT_*` env, then repeat the
  requests. Scooter 33's legacy key `legacy-33` is the same in every copy of the
  committed DB, so its sequence is fully reproducible
  (`test_a_seeded_run_replays_exactly`). A fresh start gets a new uuid key, so its draws
  differ.
- **Full reset of local state:** `make reset-local` (see Where data lives). It moves the
  ledger aside together with the DB and the records. `git restore scooter_db.json` alone
  keeps the ledger, so restored reservations find their old charges (that's intended; see
  above): a rollback, not a reset.
- **Code:** `payments.py` (`charge`, `draw`, `header_override`, `lookup`, `verify`, `main`), and
  in `app.py` the state machine: `begin_payment`, `pay`, `finish_ride`,
  `record_outstanding_balance`, `note_payment_attempt`, `abandon_payment`,
  `PAYMENT_FAILURES`. Tests: `tests/test_payments.py`, `tests/test_end_ride.py`.

## Repo map

| Path | What it is |
|---|---|
| `app.py` | The whole app. It has the Flask routes (`/`, `/view_all_available`, `/search`, `/reservation/start`, `/reservation/end`), the error handlers and `error_response`, and helpers (the payment state machine: `begin_payment`, `pay` (calls `payments.charge`), `finish_ride`, `record_outstanding_balance`, `note_payment_attempt`, `abandon_payment`, `PAYMENT_FAILURES` (outcome → kind); see Payment state machine; `calculate_cost`, `db_transaction` (lock + read for mutating requests), `init_db`, `find_db_shape_problem` / `find_reservation_problem` (DB validation), `load_reservation` / `legacy_reservation` / `new_reservation` (see Database schema), `write_db` (atomic replace, refuses invalid state), `find_scooter` (lookup + record), `get_scooter_with_id`, `convert_db_to_dictlist`, `Scooter`). Every route notes its decisions in the request record. |
| `reconcile.py` | `make reconcile`: settles stuck (`payment_pending`) rides and restore-induced charged-but-active ones, and rebuilds balances a DB rollback erased. Pure `decide()` / `plan()` plus `run()` and a CLI; see Reconcile. |
| `payments.py` | The simulated payment gateway: `charge`, the ledger, the seeded `draw`, the `X-Payment-Fault` gating, and a CLI (`make payments ARGS="lookup|draw|verify …"`, `python -m payments`). See Payments & fault injection. |
| `failures.py` | The failure-kind registry (`FailureKind`), `StorageError` and `StorageBusy` (see Failure kinds). |
| `records.py` | Request records: request ids, one JSON line per request (see Request records). |
| `records_query.py` | Records query functions and CLI (`make records ARGS=…`, `python -m records_query`). |
| `scenarios.py` | `make scenario NAME=…`: reproducible traffic on an isolated instance, with expectations per step; see Debugging a symptom. |
| `scripts/reset_local.py` | `make reset-local`: back up and reset all local state; see Where data lives. |
| `mcp_server.py`, `.mcp.json` | The `scooter` MCP server (agent tools over records, DB, ledger and tests) and its registration; see Agent tools (MCP). |
| `data/` | Default home of `records.jsonl`, `payments_ledger.jsonl` and `scenarios/`. Git-ignored except `.gitkeep`. |
| `local-backups/` | `make reset-local`'s backups. Git-ignored. |
| `scooter_db.json` | Live JSON database, mutated by the app (see above). |
| `README.md` | API contract: endpoints, params, responses. |
| `ACTIVITY.md` | The activity brief and Required outcomes 0–7. |
| `PLAN.md` | The authoritative staged plan: PR table, human decisions, change log. |
| `AGENTS.md` | This file. `CLAUDE.md` just imports it. |
| `AI_NOTES.md` | Log of where the AI was confidently wrong and how it was caught (Outcome 7). |
| `Makefile` | `setup`, `run`, `test`, `check-settings`, `records`, `payments`, `reconcile`, `scenario`, `reset-local`, `lock`, `agent`, `clean` and `help`. |
| `.claude/settings.json` | Claude Code sandbox and permission rules for this repo (see Agent boundaries). |
| `scripts/agent.sh` | Headless `claude -p` wrapper with spend, turn and wall-clock caps (`make agent`). |
| `scripts/check_settings.py`, `schemas/` | `make check-settings`: validates `.claude/settings.json` against the vendored Claude Code settings schema, and its MCP entries against `.mcp.json`. |
| `tests/`, `pytest.ini` | The pytest suite and its config (see Test). |
| `.github/workflows/test.yml` | CI: runs `make test` on PRs and on pushes to `master` (see CI). |
| `requirements.in` / `requirements.txt` | Direct runtime deps / hash-locked full runtime set. |
| `requirements-dev.in` / `requirements-dev.txt` | Direct dev deps / hash-locked dev set, constrained to the runtime lock. |
| `.python-version` | `3.15`. |
| `Procfile`, `app.json` | Heroku-style deploy config. The app is **not deployed** (PLAN PR 19 covers readiness). |

## Known behaviour worth knowing before debugging

These exist today and are scheduled in PLAN.md. Don't "fix" them outside their PR. The
ones marked *(xfail)* are pinned in `tests/test_known_bugs.py`.

- **A ride whose payment timed out or errored stays `payment_pending`** (the scooter stays
  reserved and can't be started) until someone retries `/reservation/end` or **`make
  reconcile` runs**. The server never settles rides on its own: reconcile has to be run,
  or scheduled (see Reconcile). That's where H3's "3 attempts or 15 minutes, then an
  outstanding balance" is applied.
- A retry of a pending ride ignores its own `lat`/`lng` and ends the ride where it was
  first asked to (see Payment state machine).
- `lat`/`lng`/`radius` are parsed with Python's lenient `float()`, so `+5`, `1_000`,
  surrounding whitespace and Unicode digits (e.g. `٥`) are all accepted as numbers.
- A scooter reserved before PLAN PR 12 has no record of when or by which request its ride
  started: it loads with a synthesised `legacy-<id>` reservation (see Database schema).
- Restoring a DB snapshot replays its reservations with the same ids and keys. Ending one
  again the same way returns the original charge; ending it differently is
  `payment_conflict`, and the ride goes back to `active` (see Database schema). Until it's
  ended where it was charged, that ride breaks invariant I1, and `make reconcile` settles
  it from the ledger (see Reconcile). The ledger survives `git restore`, so for a
  clean reset use `make reset-local` (see Where data lives). A restored
  ride that was **declined** is declined again (PLAN PR 15b).
- **A rollback erases rides that were in progress** (and balances with no ledger trace):
  those riders rode free, and nothing can settle them. `make reconcile` rebuilds every
  balance the ledger proves; `make records ARGS="rollback-check"` lists the rest (see
  Database schema, "What a rollback erases"). Invariant I2 flags them.
- The start response's `reservation_id` isn't used by any endpoint yet. `/reservation/end`
  still takes only `id`, `lat` and `lng`. Later PLAN PRs build on it.

## Concurrency

The JSON file DB is safe under threads, several gunicorn workers and separate processes
(code: `db_transaction()` and `write_db()` in `app.py`).

- **One transaction per mutating request.** `/reservation/start` and `/reservation/end` do
  read → check → (pay →) write inside `with db_transaction() as txn:`. That holds an
  exclusive `fcntl.flock` on the sidecar `<db>.lock` for the whole transaction, and
  resolves the DB path once.
  - `flock` locks belong to an open file description, so they exclude other threads as
    well as other processes.
  - **Why a sidecar lock file:** the write replaces the DB file (a new inode). A lock on
    the DB file itself would stop covering the path after the first write, letting two
    writers in at once.
- **Atomic writes.** `write_db` writes a temp file in the same directory, flushes and
  `fsync`s it, `os.replace`s it over the DB and `fsync`s the directory.
  - A crash mid-write leaves the old DB intact, and the temp file is removed.
  - The DB's permissions are kept, and a read-only DB is still refused (`storage_error`,
    `PermissionError`).
- **Lock-free reads.** `/view_all_available` and `/search` take no lock. `open()` sees
  either the old or the new complete file, never a torn one, so readers never wait on
  writers.
- **No silent stalls.** Waiting for the lock is bounded by `SCOOTER_DB_LOCK_TIMEOUT`
  (default 5 s).
  - A timeout returns **`storage_busy` (503, `Retry-After: 1`)**, and nothing is read or
    written.
  - Every mutating request's record has a `db_lock` decision (`acquired`/`timeout`) with
    `wait_ms` and `lock_path`, so contention shows up in the records.
  - **Lock order isn't start order.** Waiters poll the lock every 5 ms, so whichever
    retries first after a release wins; a request that started later can get the lock
    first. The `acquired` decision's `acquired_at` (UTC, microseconds, the same host's
    clock) gives the order requests took effect. Sort by it, not by `ts`, when asking
    "what was the last write". (PLAN PR 11c; a test sorted by `ts`, called it lock order,
    and flaked. `test_lock_order_not_start_order_decides_what_is_on_disk` reproduces the
    difference deterministically.)
- **Known cost, kept on purpose:** `/reservation/end` calls the payment gateway while
  holding the lock (phase 1 → charge → phase 2), so a slow gateway delays every writer.
  PLAN PR 14 chose correctness over lock-hold time; see "Payment state machine" for why
  and when to split it.
- **gunicorn `--timeout` must be longer than `SCOOTER_DB_LOCK_TIMEOUT` plus the slowest
  gateway call** (gunicorn's default is 30 s; the lock timeout's is 5 s).
  - If a worker hits gunicorn's timeout first, gunicorn kills it. The client then gets
    **gunicorn's HTML 500** (`text/html`, no `X-Request-ID`), not our JSON envelope.
  - The request record is still written, by the teardown safety net, as `internal_error`
    with `msg` "no response was produced" and `exception: {type: SystemExit}`. The
    gunicorn log shows `WORKER TIMEOUT`.
  - Verified with `--timeout 2` and a held lock with `SCOOTER_DB_LOCK_TIMEOUT=10`.
- **Directory fsync failure after a successful replace:** the new data is already in place
  and every reader sees it, so it's reported as a **successful** write, not
  `storage_error`.
  - The server log gets a WARNING, `db written but directory fsync failed …`.
  - The record keeps `db_write.ok: true`, and its `db_write` decision carries
    `dir_fsync_error: "<ExceptionType>"`.
  - The only risk is that the rename may not survive a power loss.
- **Error precedence on `/reservation/end` (changed in PLAN PR 11):** parameters are
  validated *before* the lock and the DB read. A request with a bad `lat`/`lng` and a
  corrupt DB now gets **422 `invalid_coordinate`** (or `invalid_number`), where it used to
  get 500 `storage_error`. This matches `/search`, which always validated first.
- **Verified:**
  - Threads: 8 threads racing to reserve one scooter → exactly one wins, every time.
  - Processes: 6 processes racing → exactly one wins.
  - 7 processes cycling start/end, 4 of them on the same scooter → no lost updates, no
    `storage_error`/`storage_busy`, and `anomalies` clean.
  - Readers racing writes on a 20,000-scooter DB never see a torn file.
  - A live gunicorn `-w 4` hammer (see the PLAN PR 11 PR description).

## Request records

Every request leaves **exactly one JSON line** in the records file, written when the
request finishes (success, any failure kind, 404/405, or an unhandled exception). This is
the evidence to use when debugging. Read the records before reading code.

- **Where:** `data/records.jsonl` by default (relative to the working directory), or
  `SCOOTER_RECORDS_PATH`. The directory is created on demand. `data/*` is git-ignored
  (only `data/.gitkeep` is committed). Records survive restarts: the file is only ever
  appended to.
- **Request id:** every response carries `X-Request-ID`, which is the record's
  `request_id`. A client can send its own `X-Request-ID` to find its request later. It's
  honoured only if it's 1–64 characters of `[A-Za-z0-9._-]`; anything else is replaced
  with a fresh uuid4 hex, and the record notes `request_id: client_value_rejected`.
- **Atomicity:** each record is one `write()` of one line to an `O_APPEND` descriptor,
  under an exclusive `fcntl.flock`. Lines from threads, gunicorn workers or separate
  processes never interleave. Tests cover 4 processes writing 70 KB lines, and a live
  check ran 4 gunicorn workers with 400 parallel requests.
- **If a record can't be written:** the API response is still served unchanged, plus an
  `X-Record-Error: <ExceptionType>` header. The server log gets an ERROR line,
  `REQUEST RECORD NOT WRITTEN: … (records=<path>, request_id=…)`, with the traceback.
- **Code:** `records.py`. Route code calls `records.note(step, outcome, **detail)` for each
  decision, `records.scooter(scooter_id=…, before=…, in_memory=…)` for the affected
  scooter, and `records.unchanged()` when it decides not to write. `write_db` calls
  `records.db_write(ok, error)` and `records.written(db_list)`. The latter is the only thing
  that sets `scooter_after` after a write. `records.init_app(app)` adds the before/after/teardown
  hooks. The teardown hook is a safety net that still writes a record if no response was
  produced.
- **Querying:** use the records CLI (below). jq is the fallback.
- **Not done yet:** there's no log rotation (future work). The file grows until you move
  it aside.

### Schema (`schema: 1`)

```json
{"schema": 1, "ts": "2026-11-10T01:39:40.482Z", "request_id": "135dba2cd5be4391b8a1255217a242f8",
 "method": "GET", "path": "/reservation/end", "params": {"id": "18", "lat": "23.01", "lng": "19.01"},
 "scooter_id": "18",
 "decisions": [
   {"step": "params", "outcome": "ok", "detail": {"lat": 23.01, "lng": 19.01}},
   {"step": "db_lock", "outcome": "acquired", "detail": {"lock_path": "scooter_db.json.lock", "wait_ms": 0.042,
                                                        "acquired_at": "2026-11-10T01:39:40.482613Z"}},
   {"step": "db_read", "outcome": "ok", "detail": {"db_path": "scooter_db.json", "scooters": 5}},
   {"step": "lookup", "outcome": "found", "detail": {"id": "18"}},
   {"step": "end", "outcome": "allowed", "detail": {"reason": "scooter is reserved"}},
   {"step": "payment", "outcome": "pending", "detail": {"end_lat": 23.01, "end_lng": 19.01, "distance_m": 1509, "amount": 1509}},
   {"step": "db_write", "outcome": "ok", "detail": {"db_path": "scooter_db.json"}},
   {"step": "payment", "outcome": "requested", "detail": {"distance_m": 1509, "cost": 1509, "idempotency_key": "f36f087baa0842929afe8d9eaf0e8598",
                                                         "end_lat": 23.01, "end_lng": 19.01}},
   {"step": "payment", "outcome": "ok", "detail": {"gateway_outcome": "success", "txn_id": "0d4c9e1f2a3b4c5d6e7f8091a2b3c4d5", "attempt": 1,
                                                  "fault": {"source": "env", "mode": "none"},
                                                  "simulator": {"charged": true, "ledger_txn_id": "0d4c9e1f2a3b4c5d6e7f8091a2b3c4d5"}}},
   {"step": "reservation", "outcome": "cleared", "detail": {"reservation_id": "f36f087baa0842929afe8d9eaf0e8598", "legacy": false}},
   {"step": "db_write", "outcome": "ok", "detail": {"db_path": "scooter_db.json"}}],
 "params_truncated": false,
 "scooter_before": {"id": "18", "lat": 23.0, "lng": 19.0, "is_reserved": true,
                    "reservation": {"reservation_id": "f36f087baa0842929afe8d9eaf0e8598", "started_at": "2026-11-10T01:30:02.114Z",
                                    "start_request_id": "4c1e0d6f9a2b4b7e8f3a5d6c7b8e9f01", "payment_state": "active",
                                    "idempotency_key": "f36f087baa0842929afe8d9eaf0e8598"}},
 "scooter_after": {"id": "18", "lat": 23.01, "lng": 19.01, "is_reserved": false, "reservation": null},
 "scooter_in_memory": {"id": "18", "lat": 23.01, "lng": 19.01, "is_reserved": false, "reservation": null},
 "db_write": {"attempted": true, "ok": true, "error": null},
 "status": 200, "error": null, "duration_ms": 0.81,
 "git_commit": "1b65c022a8abc8307a90eb5ef2aea6f348521818", "git_dirty": false, "git_dirty_paths": [],
 "pid": 16070}
```

(On disk each record is a single line; it's wrapped here for reading.)

| Field | Meaning |
|---|---|
| `ts` | When the request started, UTC ISO-8601 with milliseconds and a `Z`. Plain string comparison sorts and filters by time. **Not the order changes took effect:** requests wait for the DB lock by polling, so one that started later can get the lock first. For that order use the `db_lock` decision's `acquired_at` (below). |
| `request_id` | Same as the `X-Request-ID` response header. |
| `method`, `path`, `params`, `params_truncated` | The request. `params` holds the query args as strings; a repeated key becomes a list. Values over 1024 chars are truncated with `...(truncated, N chars)`. Only the first 50 keys are kept, and `params_truncated` is `true` if there were more. |
| `scooter_id` | The `id` param on `/reservation/*`, or `null`. |
| `decisions` | An ordered list of `{step, outcome, detail}`. Steps: `request_id`, `params`, `db_lock` (`acquired`/`timeout`, with `wait_ms`; `acquired` also has `acquired_at`, UTC with microseconds, when this request got the lock, i.e. the order in which mutating requests took effect (PLAN PR 11c); mutating requests only), `db_read` (`ok`/`failed`, with `db_path`), `lookup` (`found`/`not_found`), `reserve` / `end` (`allowed`/`refused`, with a reason), `payment_fault_override` (`applied`/`ignored`/`rejected`, only when an `X-Payment-Fault` header was sent), `end` (`allowed`, `refused`, or `retry` with the stored and requested coordinates), `payment` (`pending` with the end location and amount saved by phase 1, `requested` with distance, cost, idempotency key and end location, then `ok`/`failed` with `gateway_outcome`, `txn_id`, `attempt`, the `idempotency_key` and `reservation_id` the gateway actually charged under, `fault` (why that outcome) and `simulator` (ground truth: did this call take money); see Payments & fault injection), `reservation` (`created`/`cleared` with the `reservation_id`, `payment_pending` with the attempt count, `payment_abandoned` on a conflict, or `unsupported` with the unknown `payment_state`; see Database schema), `balance` (`recorded`, on a decline), `db_write` (`ok`/`failed`/`refused`), `search`, `filter`, and `respond: error` (with the kind) for every failure. |
| `scooter_before` | The scooter as read from the DB at lookup, in the full persisted form (with `reservation` and any unknown fields; see Database schema). Records written before PLAN PR 12 have no `reservation` key. |
| `scooter_after` | **What's persisted after this request.** When the request wrote, it's this scooter's entry taken from the exact data `write_db` serialised to disk, not from the code's objects. When the request decided not to write (refused, payment failed) it equals `scooter_before`. It's `null` if the write failed or the request crashed, and also whenever the request neither wrote nor declared itself unchanged (e.g. a validation error, an unknown scooter, or a code path that forgot to record its outcome; the `anomalies` query flags the last case on `/reservation/*` successes). |
| `scooter_in_memory` | What the route's in-memory scooter object looked like when it finished, i.e. what the code *believed*. **If `db_write.ok` is true and this differs from `scooter_after`, the code changed the scooter but didn't persist that change** (for example, `write_db` ran before the mutation). `null` if the route never got that far. |
| `db_write` | `{attempted, ok, error}`. `error` is the exception type. |
| `status`, `error` | The HTTP status. `error` is `{kind, msg}` on failure (see Failure kinds), otherwise `null`. |
| `exception` | Only on `internal_error` / `storage_error`: `{type, message}` of the underlying exception. The traceback is in the server log. |
| `duration_ms` | Server-side time to produce the response. |
| `git_commit`, `git_dirty`, `git_dirty_paths` | The source tree at **server startup**. `git_dirty` is `true` if any *code* file, tracked or untracked (not git-ignored), differed from `HEAD`. That's how a planted, uncommitted bug shows up, and `git_dirty_paths` lists those files (first 20). Data files the app rewrites (`scooter_db.json`) don't count. All are `null` if git isn't available. **They describe the files on disk at startup, not necessarily the code that's running:** restart the server after changing code, and delete `__pycache__/` after planting or reverting a bug (a same-second, same-size revert can leave stale bytecode in use; AI_NOTES #22). |
| `pid` | The process that served it, so you can tell gunicorn workers and restarts apart. |

### Querying records: the records CLI

**Use this first.** It's the agent's way to ask questions without reading raw JSONL, and
it needs no MCP approval. The MCP server (PLAN PR 16) reuses the same functions.

```sh
make records ARGS="failure-counts --since 1h"      # how many of each kind in the last hour
make records ARGS="recent-failures --limit 20"     # newest failures first (--since works too)
make records ARGS="scooter 18"                     # one scooter's history, in the order it took effect
make records ARGS="request <request_id>"           # the full record(s) for an X-Request-ID
make records ARGS="anomalies"                      # records whose fields contradict each other
make records ARGS="ledger-check"                   # ledger vs records: double charges, died-mid-payment requests
make records ARGS="rollback-check"                 # what a DB rollback erased (needs the DB and the ledger)
make records ARGS="--file data/scenarios/rides/records.jsonl scooter 18"   # a `make scenario` run's records
make records ARGS="--json failure-counts"          # machine-readable output (global flags go first)
.venv/bin/python -m records_query --help           # same CLI without make
```

- **File:** `--file PATH`, else `$SCOOTER_RECORDS_PATH`, else `data/records.jsonl`.
- **`--since`:** a duration (`30s`, `15m`, `1h`, `2d`, `1h30m`) or an ISO-8601 time (naive
  means UTC). The window is inclusive (`ts >= since`). A record without a valid `ts` is
  outside every window. A duration too large for a date (e.g. `99999999d`) is a bad
  argument (exit 2).
- **Output:** human-readable by default; `--json` prints exactly one JSON value on stdout.
  Warnings go to stderr.
- **Exit codes:**
  - 0: success, including "0 failures".
  - 1: `scooter`/`request` found nothing, or `anomalies` found at least one **unresolved**
    finding, like `grep` and linters. (A finding marked `"resolved": true`, so far only a
    historical `charged_and_owed`, is listed but doesn't count.) Or `ledger-check` /
    `rollback-check` found something.
  - 2: bad arguments, or the records file is missing or unreadable (for `ledger-check`
    also the ledger; for `rollback-check` the DB, or a ledger that exists but can't be
    read; a missing ledger there means no payments yet).

  `make` turns any non-zero exit into its own error (exit 2), so call
  `.venv/bin/python -m records_query` when the exact code matters.
- **Robustness:** reading streams the file line by line, so memory stays flat on large
  files. A leading byte-order mark is ignored (`utf-8-sig`). A line that isn't a complete JSON object (e.g. a partially written last line or a
  stray `42`) is skipped with `warning: <file>:<line>: skipped line, …` on stderr; the
  query still answers.
- **`anomalies`** flags eighteen contradictions. Most are within a single record;
  `pending_location_changed`, `double_charge`, `scooter_double_charge` and
  `charged_and_owed` also compare records with each other:
  - **`write_not_persisted`:** `db_write.ok` is true but `scooter_after` ≠
    `scooter_in_memory`. The code changed the scooter, but the written data didn't contain
    the change.
  - **`success_without_write`:** a `/reservation/*` request returned 200 without
    attempting a DB write. (No exemption was needed for PLAN PR 14: every end, retries
    included, writes; a retry of an already-ended ride is a 422.)
  - **`start_left_free`:** `/reservation/start` returned 200 but `scooter_after` isn't
    reserved.
  - **`end_left_reserved`:** `/reservation/end` returned 200 but `scooter_after` is still
    reserved.
  - **`end_location_not_persisted`:** `/reservation/end` returned 200 (or a `/reconcile`
    record freed the scooter) but `scooter_after`'s `lat`/`lng` differ from where the ride
    ended: the stored end location (the payment `requested` decision's `end_lat`/`end_lng`,
    since PLAN PR 14; for reconcile, its `reconcile` decision's `end_lat`/`end_lng`), or for
    older records the requested coordinates. They're compared as floats, the
    way the app parses them, so `+23.01` equals `23.01`. This one also catches code that
    never moves the scooter at all, where in-memory and written state agree.
  - **`reservation_flag_mismatch`:** `scooter_after` breaks the DB invariant
    (`is_reserved` true without a `reservation`, or false with one). `write_db` refuses
    such writes, so this flags records from older code or from a broken guard.
  - **`start_reservation_not_persisted`:** `/reservation/start` returned 200 but
    `scooter_after` has no reservation whose `start_request_id` is this request's id
    (none at all, or a stale one).
  - **`charged_without_persisted_end`:** a payment came back `ok` (the ride is paid), but a
    later `db_write` in the same request `failed` or was `refused`. The DB still says
    `payment_pending`, so a retry or reconcile settles it. This one is expected after a
    disk failure; it says which rides need settling.
  - **`decline_not_settled`:** `payment_declined`, but `scooter_after` is still reserved,
    or has no outstanding balance for that reservation **with the amount charged for** (the
    `payment: requested` cost) **and the reservation's idempotency key** (H3).
  - **`failed_payment_not_pending`:** `payment_timeout`/`payment_failed`, but
    `scooter_after`'s reservation isn't `payment_pending`, so the ride can't be settled.
  - **`ended_without_payment`:** `/reservation/end` returned 200 but the request's final
    payment result isn't `ok` (or there was none), e.g. code that treats a timeout or a
    conflict as paid. Also a `payment_declined` whose gateway outcome wasn't a decline.
  - **`charged_before_pending_persisted`:** a first-attempt end (it has a `payment: pending`
    decision) asked the gateway to charge before any `db_write: ok` after that decision.
    Phase 1 must be on disk before the charge. This shows in **every** such record, even
    when nothing crashed.
  - **`pending_location_changed`:** a retry (`end: retry`) re-priced the ride (it has its own
    `payment: pending`), or charged a different end location or amount than the one stored
    for it: the retry decision's own stored values, or any earlier record's
    `payment: pending` for the same idempotency key (looked up across the whole file, even
    outside `--since`).
  - **`payment_key_mismatch`:** the payment result says the gateway charged under an
    idempotency key (or `reservation_id`) other than the reservation's own, from
    `scooter_before`. The gateway reports what it was actually charged under, so this
    doesn't depend on what the app *meant* to send.
  - **`double_charge`:** a second, different charge (ledger `txn_id`) for a reservation
    that an earlier record (anywhere in the file) already shows charged: **one ride paid
    twice**. This is the failure class Outcome 3 most needs to rule out. `ledger-check`
    (below) finds it from the ledger too, including charges made by requests that left no
    record.
  - **`scooter_double_charge`:** two charges on one scooter between a successful start and
    the scooter becoming free, under **any** reservation or key, in the order requests took
    effect. This catches a bug that swaps in a new reservation (new id and key) before
    charging, which `double_charge` (per reservation) can't label (PLAN PR 15). A charge
    for an **older** ride that a DB rollback brought back (its start record precedes the
    scooter's latest start) starts a new count, so whole-DB rollbacks don't produce false
    positives (PLAN PR 15c); a swapped-in reservation has no start record and still counts.
  - **`charged_and_owed`:** a reservation that a record shows charged also has an outstanding
    balance recorded (in that record or any other), whatever keys the charge and the balance
    were made under: the rider paid **and** owes for one ride. (PR #20 review: a reconcile
    bug that switched keys produced exactly this, and only `payment_key_mismatch` hinted
    at it.) **Records are history, and a DB restore or reconcile can change what's owed**
    (PLAN PR 15b), so this rule also reads the current DB (`--db PATH`, default
    `$SCOOTER_DB_PATH` or `scooter_db.json`; read-only, no lock). Its detail adds
    `still_in_db` (the DB still has an **open** balance for the reservation; `null` if the
    DB couldn't be read), `db` (the DB path it consulted, or `"unknown"` when none could
    be read) and `settled_by` (the request_id of a `balance: settled` record).
    The finding is `"resolved": true` when `still_in_db` is false, or, with no readable DB,
    when a settled record exists. The human output labels it `[historical: resolved]`.
    Resolved means "no longer owed in the DB", not "never happened": the rider was
    declined and then charged for that ride.
  - **`reconcile_not_settled`:** a `/reconcile` record says it ended a ride (status 200,
    an action other than `none` or `clear_paid_balance`) but `scooter_after` is still
    reserved; or it ended one unpaid (`end_unpaid`) without a `payment_unresolved`
    balance, or declined (`settle_declined*`) without a `payment_declined` balance, for
    that reservation; or a `clear_paid_balance` left one of its balances open; or a
    `rebuild_lost_balance` (PLAN PR 15c) left no `rebuilt_from_ledger` balance for a
    reservation it named, or rebuilt one for a reservation a record shows charged.

  The reservation rules only judge snapshots that have a `reservation` key, and the
  payment-ordering rules only judge records that have a `payment: pending` decision, so
  records written before PLAN PRs 12/14 aren't flagged.

  **Tested against planted bugs** (`tests/test_planted_payment_bugs.py`): eight bugs a
  grader might plant in the end-ride state machine. Each is planted in a copy of `app.py`,
  a fault scenario is run against it, and the test requires `anomalies` to name it:

  | Planted bug | Rule that names it |
  |---|---|
  | a decline records no balance | `decline_not_settled` |
  | `timeout_not_charged` ends the ride as paid | `ended_without_payment` |
  | the phase-1 (`payment_pending`) write removed | `charged_before_pending_persisted` |
  | a retry re-prices at the request's coordinates | `pending_location_changed` |
  | the ride ends at the request's coordinates | `end_location_not_persisted` |
  | a decline keeps the ride pending | `decline_not_settled` |
  | a conflict ends the ride (a free ride) | `ended_without_payment` |
  | a decline's balance recorded as 0 | `decline_not_settled` |
  | **a new idempotency key per attempt** (a charged timeout plus a retry charges the ride twice: a **double charge**) | `double_charge`, `payment_key_mismatch` |
  | a decline's balance recorded under the wrong key | `decline_not_settled` |
  | (in `reconcile.py`) settling a charged ride under a new key: it ends charged **and** owing | `payment_key_mismatch` (since PLAN PR 15b the same run's `clear_paid_balance` voids the balance, so `charged_and_owed` is listed as resolved) |
  | (in `reconcile.py`) settling a restored ride at the scooter's current position, not the ledger's | `end_location_not_persisted` |
  | (in `reconcile.py`) `clear_paid_balance` leaves the paid balance open | `reconcile_not_settled` |
  | (in `reconcile.py`) `settle_declined_from_ledger` ends the ride without a balance | `reconcile_not_settled` |
  | (in `reconcile.py`) never finding a paid balance: the rider still owes in the DB | `charged_and_owed` (`still_in_db: true`) |
  | (in `payments.py`) a gateway that charges a restored, declined ride | `charged_and_owed` |
  | (in `reconcile.py`) `rebuild_lost_balance` writes no balance | `reconcile_not_settled` |
  | (in `records_query.lost_balances`) rebuilding a balance for a reservation charged later | `reconcile_not_settled` (the next pass voids the balance, so `charged_and_owed` is historical) |

  The same scenarios on the real app give no anomalies.

  **What `anomalies` can't see** (don't overclaim it):
  - **Requests that left no record.** A worker killed mid-request writes nothing, so no rule
    can judge that request. Use `ledger-check` (below) for the payment side.
  - **Anything not in the records.** It doesn't read the ledger, and reads the DB only to
    label `charged_and_owed`. It can't tell
    that a charge in the ledger was for the wrong amount, or that the DB was restored or
    edited by hand. That needs the cross-file invariants (`tests/invariants.py`, I1–I5)
    and `ledger-check`.
  - **Bugs that keep the records self-consistent.** For example, a `calculate_cost` that
    charges the wrong price everywhere, or a code path that records a decision it didn't
    actually take. The rules compare what a record says with itself; they don't
    recompute prices.
  - **The phase-1 rule needs the `payment: pending` decision.** Code that skips
    `begin_payment` entirely would also skip that decision; `end_location_not_persisted`
    and the tests catch that case instead.

  Tested to have zero false positives on every legitimate flow: start, end, re-reserve
  after end, refused starts and ends, ending a legacy reservation and re-reserving that
  scooter, an unknown scooter, lenient-float coordinates, every payment outcome and retry
  (pending, retry from elsewhere, charged timeout then settle, decline, conflict after a
  restore, start refused while pending), search, view, and 404; and on a 500-operation
  random-fault soak. Every rule is also tested on records from a
  planted bug.
  - **Deliberately not included: a cross-record continuity check** ("this request's
    `scooter_before` equals the previous `scooter_after`"). Concurrent workers,
    `git restore` of the DB and manual edits all break that chain legitimately, so it would
    mostly be noise. The per-record rules catch persistence bugs inside the request that
    caused them. For a manual continuity view, use `make records ARGS="scooter <id>"`,
    which prints before/after for each request in the order they took effect: by
    `acquired_at` (lock order) where the record has one, else by `ts` (PLAN PR 11c).
- **`ledger-check`** (exit 1 if it finds a problem) cross-checks the payments ledger against
  the records. Run it when a payment looks wrong, or after a crash, a deploy or a `kill -9`.
  It reports:
  - **double charges:** two or more charges for one `reservation_id`, under any keys. One
    ride paid twice; this finds it even when the requests left no record.
  - **charges under a foreign key:** a charge whose `idempotency_key` isn't its
    `reservation_id`. Every key equals its reservation id, so this is a key no reservation
    owns.
  - **open orphans:** ledger entries whose `request_id` has no request record. The request
    called the gateway and never finished (a SIGKILLed or crashed worker), and no later
    request has used that key since. `anomalies` can't see these, because there's no
    record. For each, check the key with `make payments ARGS="lookup <key>"`. The ride is
    usually still `payment_pending` (phase 1 was saved before the charge), and a retry of
    `/reservation/end` settles it with that charge. If the worker died after saving, the
    ride has already ended.
  - **resolved orphans:** the same, but a later request used the key (the retry that
    settled it). They're listed as evidence of the crash, and don't count toward exit 1.
  - `--ledger PATH` (default `$SCOOTER_PAYMENTS_LEDGER_PATH`, else
    `data/payments_ledger.jsonl`). Entries younger than `--min-age` seconds (default 5) are
    skipped, because their request may still be running. Use `--min-age 0` when nothing is
    running. `--json` gives `{open_orphans, resolved_orphans, double_charges, foreign_keys}`.
  - **Verified live:** a gunicorn worker `kill -9`'d during a `timeout_charged` payment
    left the ride `payment_pending` with a charge, no record, and 0 anomalies. The orphan
    check (then called `payment-orphans`, with the same logic) listed it, and a retry
    settled it at the stored location with the same charge. After a settle,
    `ledger-check` lists such an entry as resolved (`test_cli_ledger_check`).
- **Code:** `records_query.py`. These are pure functions over any iterable of records:
  `read_records(path, warn)`, `parse_since`, `failure_counts`, `recent_failures`,
  `scooter_history`, `find_requests`, `anomalies`, `read_ledger`, `ledger_check`,
  `ledger_problems`, `lost_balances`, `rollback_check`, `rollback_problems`.
  `main()` is the CLI.
- **`rollback-check`** (PLAN PR 15c; exit 1 if anything was erased) answers "what did the
  last DB rollback erase?" from the records, the current DB (`--db`) and the ledger
  (`--ledger`). It lists **lost balances** (the ledger proves them; `make reconcile`
  rebuilds them), **erased rides** (the records show a successful start, the DB has no
  trace and the ledger no entry: in progress when erased, so a **free ride**, not
  recoverable), **erased balances** (records-only debts, no ledger evidence), and the
  balances reconcile already rebuilt. `--since` limits it to rides started in a window.
  See Database schema, "What a rollback erases".

**Fallback: jq.** These recipes are verified with jq 1.7 (`jq` ships with macOS).

```sh
R=data/records.jsonl     # or "$SCOOTER_RECORDS_PATH"

# Follow one request (id from the X-Request-ID header):
jq 'select(.request_id == "probe-42")' $R
grep '"request_id": "probe-42"' $R                       # same, without jq

# One scooter's history, roughly oldest first. File order is completion order (several
# workers can finish out of order). Sorting by ts gives START order, which can differ from
# the order changes took effect (lock order); `make records ARGS="scooter 18"` sorts by
# the db_lock decision's acquired_at, so prefer it when order matters:
jq -s -c 'map(select(.scooter_id == "18")) | sort_by(.ts)[] | {ts, path, status, kind: .error.kind,
       before: .scooter_before.is_reserved, after: .scooter_after.is_reserved}' $R

# Requests where the code's view and what was written disagree (a persistence bug):
jq -c 'select(.db_write.ok == true and .scooter_after != .scooter_in_memory)
       | {ts, request_id, path, after: .scooter_after, in_memory: .scooter_in_memory}' $R

# Failures by kind in the last hour (BSD date first, GNU date as fallback):
SINCE=$(date -u -v-1H +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S)
jq -r --arg since "$SINCE" 'select(.ts >= $since and .error != null) | .error.kind' $R | sort | uniq -c

# Recent failures, and which code served them:
jq -c 'select(.error != null) | {ts, request_id, path, kind: .error.kind}' $R | tail -20
jq -r '[.git_commit[0:12], .git_dirty] | @tsv' $R | sort | uniq -c
```

To produce evidence while reproducing a symptom, run the server against scratch copies
(see Configuration), make the requests, then query the scratch records file.

## Failure kinds

Every error response is JSON with a human message and a **stable failure kind**:

```json
{"msg": "Error 422 - No scooter with id 42 was found.", "error": {"kind": "scooter_not_found"}}
```

- **Contract:** `msg` is kept byte-for-byte as before (README compatibility), except where it
  was wrong: a non-numeric radius said "Lat/Lng values must be numbers" until PLAN PR 8. `error.kind`
  is what code and agents should match on. The ids are a contract: **once an id ships, it
  is never renamed or reused**. A new failure mode gets a new id.
- **Where they live:** the registry is `failures.py` (`FailureKind`, a `StrEnum` whose
  values are the ids, each with a default HTTP status and a description). App code returns
  errors only through `error_response(kind, msg)` in `app.py`.

| `error.kind` | HTTP | Meaning |
|---|---|---|
| `missing_param` | 422 | A required query parameter is missing. |
| `invalid_number` | 422 | `lat`, `lng` or `radius` isn't a number. The msg is "Lat/Lng values must be numbers" for lat/lng, and "Radius must be a number" for radius (since PLAN PR 8; before that, radius got the Lat/Lng msg). |
| `invalid_radius` | 422 | `radius` is a number but negative or not finite (`inf`, `-inf`, `nan`). It's in metres; `0` is allowed (exact location) and there's no upper bound (anything over ~20,004 km covers every scooter). |
| `invalid_coordinate` | 422 | Latitude outside [-90, 90] or longitude outside [-180, 180]. |
| `scooter_not_found` | 422 | No scooter has the given id (ids are matched exactly; `18 ` isn't `18`). |
| `scooter_already_reserved` | 422 | Start requested for a scooter that's already reserved. |
| `no_active_reservation` | 422 | End requested for a scooter that isn't reserved. |
| `scooter_payment_pending` | 422 | Start requested for a scooter whose last ride is `payment_pending` (its payment hasn't settled). Retry ending that ride, or run `make reconcile`. |
| `payment_failed` | 502 | The payment gateway reported an **error** (outcome `error`), or its ledger is unreadable (it fails closed: no charge). The ride is **`payment_pending`**: retry the end to settle it. Body adds `ride_ended: false`. The status is always 502: the old pass-through of a gateway's own `msg`/`code` went away with the dummy gateway (PLAN PR 13). |
| `payment_declined` | 422 | The gateway declined the payment. **The ride ended** (H3): the scooter is free at the end location, nothing was charged, and the cost is recorded in the scooter's `outstanding_balances`. Body adds `ride_ended: true` and `outstanding_balance` (H7). |
| `payment_timeout` | 504 | The gateway timed out (`timeout_not_charged` or `timeout_charged`; the client can't tell which, the record's `simulator.charged` can). The ride is **`payment_pending`**; a retry settles it and is charged at most once. Body adds `ride_ended: false`. |
| `payment_conflict` | 409 | The reservation's idempotency key already has a charge for a different ride (scooter, reservation, end location or amount differ): a restored DB ended elsewhere, even at the same distance. Nothing is charged; the ride is `active` again (not ended, not pending). Body adds `ride_ended: false`. The record's `existing_charge` shows the earlier charge. |
| `route_not_found` | 404 | No endpoint at that path. |
| `method_not_allowed` | 405 | Wrong HTTP method (every endpoint is GET); the `Allow` header is kept. |
| `reconcile_unsettled` | 409 | **Not an API response:** recorded on a `make reconcile` `/reconcile` record when reconcile leaves a ride unchanged for a human (a charge that doesn't fit, an unreadable ledger, a conflict). The msg says which scooter and why, so `make records ARGS="recent-failures"` / `failure-counts` show it. |
| `storage_busy` | 503 | A mutating request (`/reservation/*`) couldn't get the DB lock within `SCOOTER_DB_LOCK_TIMEOUT` (default 5 s), because another request held it. **Nothing was read or written**, so it's safe to retry; the response has `Retry-After: 1`. The record has a `db_lock: timeout` decision with `wait_ms`. A separate kind from `storage_error` because it's transient and retryable, and agents counting failures should see "contention" apart from "broken DB". |
| `storage_error` | 500 | `scooter_db.json` (or `SCOOTER_DB_PATH`) is missing, unreadable, corrupt or unwritable, or has the wrong shape: the top level must be a list of objects, each with a unique string `id`, finite-number `lat`/`lng` in range and a boolean `is_reserved`, plus a valid `reservation` consistent with `is_reserved` when that key is present, and unique reservation ids and idempotency keys (see Database schema; extra fields are allowed and kept). Also: start/end on a scooter whose reservation is in a `payment_state` this server version doesn't handle (only that scooter is affected). The msg names the cause without a path, e.g. `(FileNotFoundError)` or `(entry 3: lat is not a finite number)`. The **server log** line `storage error: … (db=<path>)` carries the path and the original exception's traceback. |
| `internal_error` | 500 | Anything else, i.e. a bug, including `abort(500)` and a write refused because the code produced an invalid DB state (`db_write.error: "InvalidDbState"`). The msg is generic; the **traceback is in the server log** (`unhandled exception`). Any other HTTP error raised by the framework (unreachable today) also gets this kind but keeps its own status. |

- **How it's wired (Flask 3):** a handler for `StorageError`, one for `HTTPException`
  (404, 405 and `abort(...)`, including `InternalServerError`, which is itself an
  `HTTPException`), and a catch-all for `Exception`. Because the catch-all is registered
  for `Exception` rather than `500`, it also runs with `TESTING=True`.
- **Counting failures:** group by `error.kind`, never by parsing `msg`. Every request
  record carries the same `error.kind`: `make records ARGS="failure-counts --since 1h"`
  (see "Request records").
- **Tests:** `tests/test_failure_kinds.py` pins the shipped ids and statuses and produces
  **every** kind through a real request. Adding a kind to `failures.py` without adding it
  to `SHIPPED` and `TRIGGERS` there fails the suite. Update this table and README.md in the
  same PR.

## Agent tools (MCP)

`mcp_server.py` (PLAN PR 16, ACTIVITY Outcome 4) is an MCP server named **`scooter`** that
lets an agent ask "what failed recently?", "what happened to scooter 18?" or "run the
tests" as tool calls. It's built on the official `mcp` Python SDK **2.x**
(`mcp.server.mcpserver.MCPServer`; the 1.x `mcp.server.fastmcp` import is gone) and runs
over stdio. Every tool is a thin wrapper over the same pure functions as the CLIs
(`records_query`, `reconcile`, `payments`, `tests/invariants.py`).

**Enabling it in a fresh session.**
- `.mcp.json` (checked in) defines the server: `.venv/bin/python mcp_server.py`, run from
  the repo root. Run `make setup` first, so `.venv` has `mcp`.
- `.claude/settings.json` approves it by name (`enabledMcpjsonServers: ["scooter"]`). That
  approval only counts **after you accept the workspace trust dialog**: a cloned repo
  can't approve its own servers, so until then `claude mcp get scooter` shows
  `⏸ Pending approval`. Start `claude` in the repo root, accept the dialog, then run
  `/mcp`: `scooter` should be connected, with 15 tools.
- To check it without an interactive session (no model call):
  `claude --settings '{"enabledMcpjsonServers":["scooter"]}' mcp get scooter` →
  `Status: ✔ Connected`.

**Tools.** Every result has `result` and `files_read` (the absolute path of each file it
read, and whether it exists). The data paths are the app's: `SCOOTER_DB_PATH`,
`SCOOTER_RECORDS_PATH` and `SCOOTER_PAYMENTS_LEDGER_PATH`, defaulting to the live files,
with relative paths resolved against the repo root. **Every read-only tool and
`replay_fault` also take `scenario="<name>"`** (PLAN PR 17): they then read the latest
`make scenario NAME=<name>` run in `data/scenarios/<name>/` (or `$SCOOTER_SCENARIOS_DIR`)
instead, so a scenario's evidence is one tool call away. `reconcile_plan`/`reconcile_run`
act on the live files only. A missing records file means "no
records yet". A bad argument or an unreadable file is a **tool error** (`is_error`, with
a message saying what to fix); the server keeps running.

| Tool | Approval | Purpose | CLI fallback |
|---|---|---|---|
| `recent_failures(since?, limit?)` | none | the newest failed requests | `make records ARGS="recent-failures"` |
| `failure_counts(since?)` | none | failures per `error.kind` | `make records ARGS="failure-counts"` |
| `scooter_history(scooter_id)` | none | one scooter's records, in the order they took effect | `make records ARGS="scooter <id>"` |
| `request_trace(request_id)` | none | the full record(s) for one `X-Request-ID` | `make records ARGS="request <id>"` |
| `scooter_state(scooter_id \| "all")` | none | the DB's current state (reservation, payment state, balances), validated like an app read | `cat scooter_db.json` |
| `anomalies(since?)` | none | records that contradict themselves; `unresolved` is what needs attention | `make records ARGS="anomalies"` |
| `ledger_check(min_age_s?)` | none | ledger vs records: double charges, foreign keys, orphans | `make records ARGS="ledger-check"` |
| `rollback_check(since?)` | none | what a DB rollback erased (lost balances, free rides) | `make records ARGS="rollback-check"` |
| `check_invariants()` | none | I1–I5 across DB, ledger and records | `.venv/bin/python tests/invariants.py` |
| `payment_lookup(idempotency_key)` | none | every ledger entry for one key | `make payments ARGS="lookup <key>"` |
| `payment_draw(seed, idempotency_key, attempt, rate?)` | none | recompute a seeded random fault | `make payments ARGS="draw …"` |
| `reconcile_plan(max_attempts?, max_age_minutes?, only?)` | none | what `make reconcile` would do (a dry run) | `make reconcile ARGS="--dry-run"` |
| `run_tests(paths?, keyword?, exitfirst?, timeout_s?, tail_lines?)` | **asks** | runs pytest (as `make test`), or a validated subset: test files under `tests/` (`tests/test_x.py[::test_name]`), a `-k` expression of plain words, `-x`. No other option, no shell. Timeout ≤ 900 s (the process group is killed), output tail ≤ 400 lines; no nested runs | `make test ARGS="…"` |
| `replay_fault(request_id, fault?)` | **asks** | reproduces a recorded `/reservation/end` payment failure on an **isolated temp instance** (below) and returns the replayed record(s) and whether the outcome matched | the Payments recipes |
| `reconcile_run(max_attempts?, max_age_minutes?, only?)` | **asks** | **changes the live DB**: `make reconcile` | `make reconcile` |

- **Why the three action tools ask** (`ask` rules in `.claude/settings.json`; the 12
  read-only tools are `allow`ed): MCP servers run **outside the Bash sandbox**, with your
  user's full access. `run_tests` runs code (the suite's fixtures never touch the live
  files, and the conftest guards fail a test that does). `replay_fault` runs the app.
  `reconcile_run` writes the live DB. The read-only tools only read the data files.
  `tests/test_settings_policy.py` checks that every read-only tool has an allow rule,
  every action tool an ask rule, and every rule names a real tool. `make check-settings`
  checks that `enabledMcpjsonServers` and every `mcp__…` rule name a server in `.mcp.json`,
  and that the server's command exists.
- **Trust boundary (read this before approving anything).** The server is started by Claude
  Code **outside the Bash sandbox**, from whatever `.mcp.json` says, and approved by
  **name** (`enabledMcpjsonServers`). So:
  - **`.mcp.json` and `mcp_server.py` are guarded** (PR #23 review): edits to either through
    Claude's file tools **ask** (`Edit(/.mcp.json)`, `Edit(/mcp_server.py)`), and
    `.mcp.json` is in the sandbox's `filesystem.denyWrite` (Claude Code also protects
    `.mcp.json` by default; the explicit entry is pinned by a test). A sandboxed
    `git checkout` / `git pull` that changes `.mcp.json` therefore fails with `unable to
    unlink old`; approve the unsandboxed retry or run git yourself. `.mcp.json` rarely
    changes, so that's acceptable. `mcp_server.py` is **not** in `denyWrite`, because every
    branch that touches the server would hit the same failure; a sandboxed shell write to
    it is only caught in review.
  - **`run_tests` runs the working tree's test code** (`tests/*.py`, `conftest.py`) outside
    the sandbox once approved, and the approval prompt shows only the arguments, not the
    code. **Prefer `make test`** (it runs sandboxed), and approve `run_tests` only for tests
    you've reviewed. It drops every `PYTEST_*` and `PYTHON*` variable (e.g.
    `PYTEST_ADDOPTS`, `PYTHONPATH`) from pytest's environment, so the validated argument
    list can't be widened from outside.
  - **Residual risk, not fixed: the auto-approved read-only tools run working-tree code
    too.** The server imports `app.py`, `records_query.py`, `reconcile.py`, `payments.py`,
    `records.py` and `failures.py` when it starts, and `check_invariants` loads
    `tests/invariants.py` on every call. An ordinary edit to one of them (no prompt, since
    sandboxed edits to code are allowed) therefore runs **unsandboxed** on the next server
    start or `check_invariants` call. Wrapping the server in an OS sandbox
    (`sandbox-exec` / `bwrap`) is an open human decision, **H11** in PLAN.md. Until then:
    restart the server (`/mcp`) only on code you've reviewed, and treat a working tree
    with unreviewed changes to those files as untrusted for MCP use.
- **`replay_fault` isolation.** It reads the record, the live DB and the live ledger, and
  writes only to a fresh temp directory (deleted afterwards): a DB that is the current
  one with that scooter put back as the record saw it (`scooter_before`), the key's
  ledger entries from before that attempt (so attempt numbers and ledger outcomes
  replay), and an empty records file. It then re-applies the record's fault provenance:
  `PAYMENT_FAULT` for `env`, `ENABLE_FAULT_INJECTION=1` + the header for `header`,
  `PAYMENT_SEED`/`PAYMENT_FAULT_RATE` for a seeded draw, nothing for `ledger`. The end
  request runs in a subprocess (Flask test client) whose env points all three paths at the
  temp dir, so it can't reach the live files, and a guard refuses to run if any temp path
  is a live one. `fault=` forces another outcome instead ("what if it had declined?").
- **Fallback:** everything is also a CLI (table above) that needs no MCP approval.
- **Tests:** `tests/test_mcp_server.py` drives the server through a real MCP client
  session (the SDK's in-process transport, plus one stdio subprocess as `.mcp.json` runs
  it): the tool list, every tool on fixture data, tool errors (and the server still
  answering), `replay_fault` reproducing env, header and seeded faults without changing
  any data file (and its guard), and `run_tests` argument validation, timeout and tail.

## Agent boundaries

### Escalation policy (decided by the repo owner)

> "I don't want every push, PR creation, merge, and comment to need human intervention;
> only when a decision is unclear from documentation or if agents disagree on the solution
> do I want human intervention."

So agents **act autonomously within the rules below**: branch, commit, push feature
branches, open, comment on, review and squash-merge PRs, read anything through `gh api`,
and edit code, `Makefile`, `requirements*` and `scripts/`. They **escalate to a human
only** in two cases:

1. **The docs don't settle a decision.** The docs are README.md, ACTIVITY.md, PLAN.md
   (including its Human decisions table) and this file.
2. **The writer and the reviewer disagree** on the solution.

Escalating means stopping and asking the human in chat, and saying which document left the
question open or what the two positions are.

The few prompts that remain (below) are for things the policy doesn't cover:
- changes to the guardrails themselves
- **GitHub API writes** (`gh api` with `-X`/`--method`, `-f`/`-F`/`--field`/`--raw-field`,
  `--input`). The owner's list is push, PR creation, merge and comment, and API writes
  aren't on it.
- unsandboxed commands
- destructive local git

The boundaries live in three places:

- **`.claude/settings.json`**: committed, project scope, the Claude Code sandbox plus
  permission rules. It applies to every Claude Code session whose working directory is the
  repo root, including worktrees of this repo.
- **`scripts/agent.sh` / `make agent`**: the budget, turn and time wrapper for headless
  runs.
- **GitHub settings that only a human should change:** branch protection (H2). Agents
  are denied the `gh api` calls that would change it, but that's a text-match rule; see
  below. To **check** whether it's on, use this (allowed):
  `gh api repos/Kiwi-223/scooterAPI-reliability-TI26/branches/master --jq .protected`.
  The `…/branches/master/protection` endpoint itself is denied, even for reads.

The settings use only keys documented at <https://code.claude.com/docs> (settings
reference, permissions, sandboxing) for Claude Code 2.1.310.

**Validate after every edit with `make check-settings`.** CI runs it too.

- **What it checks:** `.claude/settings.json` against the SchemaStore Claude Code settings
  schema, vendored at `schemas/claude-code-settings.schema.json`. The source URL, fetch
  date and sha256 are in `scripts/check_settings.py`. The script also makes the top level
  strict. It fails on:
  - an unknown or misspelled key at any level that matters (top level, `permissions`,
    `sandbox`, `sandbox.network`, `sandbox.filesystem`)
  - a wrong type or enum value
  - a permission rule that doesn't fit the rule pattern, such as an unknown tool name or
    an unbalanced parenthesis
- **Schema tests:** `tests/test_settings_schema.py` plants each of those mistakes and
  checks that they're rejected.
- **Behaviour tests:** `tests/test_settings_policy.py` tests what the rules *do*, not the
  rule strings.
  - **Matcher:** a small matcher implements the documented rule semantics: `*` matches
    anything including spaces; a trailing ` *` also matches the bare command; a trailing
    `:*` means ` *`; deny > ask > allow; and compound commands are split into parts.
    Tests check it against the docs' own examples.
  - **Expected outcomes:** about 100 hand-written example commands, plus every generated
    `f`/`d` short-flag cluster of up to three letters in three push positions, each with
    its expected outcome.
    - **allow:** the normal workflow.
    - **deny:** mirror/all/prune/delete/force pushes in plain, abbreviated and quoted
      spellings, pushes to `master`, and branch-protection and DELETE API calls.
    - **ask:** API writes, guardrail edits, destructive git.
    - **default:** unlisted commands.
  - **Pinned:** `excludedCommands` and `allowedDomains` exactly (so widening either one
    fails a test), the sandbox and bypass settings, the guardrail asks and the secret
    reads. **Add a row whenever you change a rule.**
  - **Limits:** the matcher doesn't model wrapper stripping, the built-in read-only set
    or quote-aware splitting.
- **`claude doctor` is not a validator.** It reports malformed JSON, wrong value types and
  some rule errors. It stays **silent on unknown keys** (for example a misspelled sandbox
  key) and on many malformed rules (AI_NOTES #11).
- **Stale schema:** SchemaStore can lag behind new CLI releases. If the check rejects a key
  that the docs document, re-fetch the schema and update the date and hash in the script.

> **Honesty first:** permission rules match the command *text*. They're a speed bump for
> the usual spelling of a command, not a security boundary. The sandbox is OS-enforced
> (Seatbelt on macOS, bubblewrap on Linux) and is the real control for files and network
> inside a session.
>
> **Unverified: is the sandbox actually on?** The writer and reviewer subagent sessions in
> this repo were observed running **unsandboxed**: `touch ~/…` succeeded and
> `curl example.com` returned 200. So the "OS-enforced" rows below describe the configured
> policy, not something verified in those sessions. Confirm in an interactive session
> with `/sandbox` and `/status`. The human is looking into it.
>
> **Merging to `master` is now automated:** agents squash-merge their own PRs without a
> human. The only things that gate what reaches `master` are the adversarial reviewer's
> verdict (a process rule, not a technical control), CI, and GitHub branch protection.
> Branch protection makes the `test` check required and blocks direct and force pushes.
> **It is not enabled yet (H2).** Until it is, nothing technical stops a merge with red CI
> or a push to `master` spelled in a way the deny rules don't catch.

### Blast radius: the worst a confused agent could do here

- **Rewrite shared history:** push to `master` or force-push, losing work.
- **Merge something bad:** agents merge their own PRs by design, so a wrong change can land
  on `master` without a human seeing it. Today's safeguards are the reviewer's verdict and
  CI. CI only *blocks* a merge once branch protection (H2) is on.
- **Disable the safety net:** remove branch protection or rulesets through the GitHub API.
  The `gh` token belongs to the repo owner, so it has admin rights.
- **Destroy uncommitted work** in this shared checkout (other agents work here too):
  `rm -rf`, `git reset --hard`, `git clean`. Or kill another agent's server.
- **Leak credentials** such as the `gh` token, `~/.ssh` or git credentials, by sending them
  to an arbitrary host.
- **Weaken its own guardrails** by editing `.claude/settings.json` or `.github/` (these
  still ask first). It could also edit `Makefile` or `scripts/` (allowed) and then run the
  edited code; what `make` runs stays inside the sandbox.
- **Burn money:** a headless run that loops forever.

### What's enforced, and how strongly

| Control | Mechanism | Strength / known bypasses |
|---|---|---|
| **Writes** limited to this checkout and `$TMPDIR` | Sandbox (`sandbox.enabled`, `failIfUnavailable: true`) | **OS-enforced when the sandbox is running** (unverified for subagent sessions; see the note above) for every Bash command and its children: `python -c`, `make` and anything else. Inside the checkout, the sandbox still protects `.claude/settings*.json`, `.git/hooks` and `.git/config`. Bypasses: Claude's own Edit/Write tools run outside the sandbox and follow permission rules instead; excluded commands (below); approved unsandboxed retries. |
| **Network** limited to `pypi.org`, `files.pythonhosted.org`, `github.com`, `api.github.com`, `localhost`, plus the docs hosts `code.claude.com`, `docs.github.com`, `endoflife.date`, `raw.githubusercontent.com` and `json.schemastore.org` | Sandbox proxy (`network.allowedDomains`) | **OS-enforced** for all sandboxed processes, including `python -c "urllib..."`. Per the docs, any other host prompts in Manual / `acceptEdits` mode and is refused in `dontAsk` mode, in auto mode (unless the classifier approves), and in `make agent`. Not exercised here. Residual risk: every allowlisted host can receive data (even a GET carries a URL), so data could still be sent *to GitHub* or a docs host. `raw.githubusercontent.com` serves any public repo's files, so sandboxed code can download arbitrary content (as `git clone` from `github.com` already can). The proxy doesn't inspect TLS. The WebFetch tool isn't sandboxed; it follows its own permission prompts. |
| **Localhost** (`make run` + `curl localhost:8080`) | `network.allowLocalBinding: true` (macOS) | Needed for manual endpoint checks. Trade-off: sandboxed commands can reach **any** service listening on localhost. |
| **Secrets** unreadable: `.env*`, `*.pem`, `*.key`, `~/.ssh`, `~/.aws`, `~/.netrc`, `~/.config/gh`, `~/.git-credentials` | `Read(...)` deny rules, which Claude Code also feeds into the sandbox's `denyRead` | Enforced for the Read tool **and** OS-enforced for sandboxed commands. It doesn't cover the excluded `gh` / `git push` commands, which need their credentials. |
| **Unsandboxed retry** (`dangerouslyDisableSandbox`) needs a human | ask rule `Bash(dangerouslyDisableSandbox:true)` | This prompt also appears in auto and bypass modes. Headless (`make agent`) can't answer it, so the retry is denied. |
| **No bypass mode** | `permissions.disableBypassPermissionsMode: "disable"` | `--dangerously-skip-permissions` is rejected in this repo. |
| **Allowed without a prompt** (escalation policy): pushes of the shape `git push [-u\|-q\|--set-upstream] origin stage-…`; `gh pr create/comment/edit/review`; `gh pr merge --squash`; `gh api` **reads**; edits to `Makefile`, `requirements*` and `scripts/**` | `allow` rules | Deny and ask rules below still win over these. Any other push shape (a bare `git push`, `git push origin HEAD`, other branch prefixes), other merge styles (`--merge`, `--rebase`) and `gh pr close` have no allow rule, so the session's permission mode decides (Manual prompts; auto mode asks the classifier, not a human). `git -C <path> …` has no allow rule either: it isn't excluded, so it runs **sandboxed** and the sandbox auto-approves it. |
| **Asks first:** `gh api` writes (`-X`/`--method` anything, `-f`/`-F` incl. `--field`/`--raw-field`, `--input`), edits to `.claude/**` (an agent can't loosen its own boundaries), `.github/**` (CI integrity) or `.mcp.json` / `mcp_server.py` (what the unsandboxed MCP server runs), any unsandboxed retry, and `git -c`, `git reset --hard`, `git clean`, `git branch -D` | `ask` rules | Text match only. File-edit asks cover Claude's file tools and recognised shell writes (`> file`, `sed`, `tee`), but not, for example, `python -c "open('.github/x','w')"`. `.claude/settings*.json` is **also** write-protected by the sandbox, which is OS-enforced. |
| **Denied (`git push`):** every dangerous flag in `git push -h` (git 2.54), matched anywhere in the command, including git's unique-prefix abbreviations: `--force`/`--force-with-lease`/`--force-if-includes` (`--for…`), `--mirror` (`--mi…`), `--all` (`--al…`), `--branches` (`--br…`), `--prune` (`--pru…`), `--delete` (`--de…`), `--receive-pack`/`--exec` (they run a program). Also any short-flag cluster in which `f` or `d` appears within its first three letters, mixed with `u q v n` (e.g. `-f`, `-uf`, `-qud`, `-fquv`; PLAN 6b), a `+` refspec, a `:branch` delete refspec, any `refs/…` refspec, **any mention of `master`**, and `git -C … push` | `deny` rules | Text match only, **defence in depth, bypassable.** Not caught: a plain `git push` / `git push origin HEAD` while `master` is checked out (no allow rule, so the mode decides); short-flag clusters where `f`/`d` first appears at the 4th letter or later (`-uqvf`) or that mix in `-4`/`-6` (`-4uf`); for those, the real control is branch protection (H2) plus PR review; quoted or escaped spellings, because the rules match the raw command text. The substring rules (`master`, `refs/`, long flags) still catch whole-token quotes such as `'HEAD:master'` or `"--mirror"`, but split quotes (`--mi"rror"`, `"mas"ter`) defeat them. The short-flag rules are defeated by **any** quoting or escaping of the flag (`'-f'`, `"-uf"`, `-"f"`, `-\f`), and by a tab instead of a space before it. Also not caught: aliases, scripts and `sh -c`. Side effect: a branch whose name contains `master` can't be pushed. |
| **Denied (other):** `gh pr merge --admin`, `gh repo delete`, `gh release delete`, `gh api` calls touching `*protection*` / `*rulesets*` or containing `DELETE` (any of `-X DELETE`, `-XDELETE`, `--method DELETE`, `--method=DELETE`), `gh auth *` (would print the token), `rm -rf` (common spellings) | `deny` rules | Text match only. A lowercase `-X delete` is still an API write, so it asks rather than being denied. `rm -rf` isn't caught as `/bin/rm -rf` or `find -delete`. |
| **`master` can't be pushed or force-pushed, and merges need green CI** | GitHub branch protection: require PRs, require the `test` check, block force-push | **Not enabled.** It's a human decision (H2), and agents are denied the API calls that change it. With merges automated, this is **the** control on `master`. Turn it on before relying on autonomous merges. |
| **The `scooter` MCP server** runs outside the sandbox, approved by name | ask rules on its action tools and on edits to `.mcp.json` / `mcp_server.py`; sandbox `denyWrite` on `.mcp.json` | The entry point is guarded, but the read-only tools (allowed) import working-tree modules, so an unprompted edit to `app.py` etc. runs unsandboxed on the next server start. **Open human decision H11** (see Agent tools (MCP), "Trust boundary"). |
| **Spend and time caps** for headless runs | `make agent` → `scripts/agent.sh` | `--max-budget-usd` is a client-side estimate, so spend can pass it a little. The wall clock is enforced by the script's watchdog. Interactive sessions have no cap from this repo. |

### The specific bypasses, and whether they're closed

- **`python -c` to reach the network (or read secrets): closed for sandboxed commands.**
  The sandbox applies to child processes, so Python goes through the same proxy and
  `denyRead`.
  - **Still open:** allowlisted hosts, and anything that runs outside the sandbox:
    the excluded `gh *` / `git push *` (feature-branch pushes and `gh api` reads are
    allowed without a prompt), an approved unsandboxed retry, Claude's WebFetch tool,
    hooks and MCP servers (including this repo's `scooter` server: its action tools ask;
    see Agent tools (MCP)).
  - **Stricter option:** drop `github.com` / `api.github.com` from `allowedDomains` and
    also exclude `git fetch *` / `git pull *`. Then only excluded, permission-checked
    commands can reach GitHub.
- **`git push origin HEAD:master`: caught only by text matching.**
  - The deny rules match it (any push mentioning `master`, quoted or not), and
    `git -C … push` is denied outright. Only the `git push … origin stage-…` shape is
    pre-approved; other shapes fall to the permission mode.
  - **Still open:** a script, an alias, or quote-split spellings (`"mas"ter`). A plain
    `git push` while `master` is checked out isn't pre-approved, but auto mode's classifier
    (not a human) decides it.
  - **The real fix is branch protection (H2).**
- **Edit a module the MCP server imports, then let the server run it: open (H11).** Edits
  to `app.py`, `records_query.py` and the rest are allowed and unprompted, and the
  `scooter` server (unsandboxed) imports them on its next start. `.mcp.json` and
  `mcp_server.py` edits ask, and `.mcp.json` is write-denied in the sandbox, so the
  server's command can't be swapped silently; its imported code can. See Agent tools
  (MCP), "Trust boundary".
- **Edit the `Makefile`, then run `make`: allowed by policy, contained by the sandbox.**
  - Editing `Makefile`, `requirements*` and `scripts/**` is allowed, because later PRs
    need it routinely.
  - Whatever `make` then runs is **still sandboxed**, so it gets the same write, network
    and secret limits.
  - The change shows up in `git diff`, in the reviewer's check and in CI.
  - `scripts/agent.sh` is the one exception that matters: editing it can change the caps
    for *future* headless runs. That's visible in review, not prevented.
  - Adding `./Makefile` to `sandbox.filesystem.denyWrite` would close this, but sandboxed
    `git checkout` / `git pull` would then fail whenever a branch changes the Makefile.

### Permissions, in short

| Rule | Commands |
|---|---|
| **Allow** (no prompt) | the 12 read-only `scooter` MCP tools (`mcp__scooter__…`); `make help/setup/test/run/lock/check-settings/clean`; read-only `git` (`status`, `diff`, `log`, `show`, `rev-parse`, `fetch`); `git push [-u\|-q\|--set-upstream] origin stage-…`; `gh pr view/list/diff/checks/create/comment/edit/review`; `gh pr merge --squash`; `gh run list/view/watch`; `gh api` reads; edits to `Makefile`, `requirements*`, `scripts/**` |
| **Ask** | everything in the "Asks first" row above, plus the `scooter` MCP tools `run_tests`, `replay_fault` and `reconcile_run`, and edits to `.mcp.json` and `mcp_server.py` |
| **Deny** | the "Denied" row above, plus the secret reads |

With the sandbox on and `autoAllowBashIfSandboxed: true`, most other sandboxed commands run
**without** a prompt: the sandbox boundary replaces the prompt. Deny and ask rules still
apply. In a `claude -p` run, project **allow** rules aren't applied, because the workspace
is never "trusted" there. `scripts/agent.sh` passes its own `--allowed-tools` for that
reason.

**Workspace trust:** the first interactive session in this repo shows the workspace trust
dialog. The project allow rules only apply once you accept it.

### Commands that run outside the sandbox (`excludedCommands`)

- **`gh *`:** on macOS, Go programs like `gh` can fail TLS verification under Seatbelt
  (documented limitation).
- **`git push *`:** so it can use the keychain credential helper. The `stage-…` push
  shape is allowed without a prompt, and the push deny rules above still apply.

Excluded commands run with your full access, subject only to the permission rules above.
Because `gh *` is excluded, an approved `gh api` call can reach any GitHub endpoint the
owner's token can reach. Reads are pre-approved. Writes ask, and protection, ruleset and
`DELETE` calls are denied, all by text.

**An exclusion applies only when the whole Bash call is that one command.** A loop, pipe,
`cd`, redirect or `$(...)` keeps the call sandboxed, and then `gh` may fail TLS on macOS.
So run `gh ...` and `git push ...` as **plain single commands**: no `for` retry loops, no
`| head`. If one fails, re-run it. This replaces the retry-loop advice from before the
sandbox.

`claude -p` and any session with the sandbox on also block writes outside the checkout. So:

- pip can't write its cache under `~/Library/Caches`. It warns and carries on.
- gunicorn needs `--no-control-socket`; see Run.
- **Unverified:** whether a harness scratchpad directory outside the checkout (for example
  under `/private/tmp/claude-<uid>/`) is writable from sandboxed Bash. Only the checkout
  and `$TMPDIR` are documented as writable. Claude's Write tool isn't sandboxed and isn't
  affected.

### Isolation

- **Branches:** every change happens on a `stage-<N>/<slug>` branch and lands through a
  reviewed, squash-merged PR.
- **Worktrees:** the reviewer works in a separate worktree. `make agent WORKTREE=<name>`
  runs the headless agent in `.claude/worktrees/<name>` (git-ignored) on its own branch,
  so it can't touch this checkout's working tree.
- **Sandbox:** see above.
- **No container or VM.** The Claude Code process itself, its file tools, hooks and MCP
  servers run with your user's full access.

### Reversibility

| What went wrong | How to undo it |
|---|---|
| A bad change on a branch | Push a fix commit. Never force-push. |
| A bad merge on `master` | `git revert <sha>` in a new PR. Squash merges make that one commit. |
| Mutated local state (DB, records, ledger) | `make reset-local`: backs everything up to `local-backups/<timestamp>/` first, then resets; undo by moving the files back. (`git restore scooter_db.json` alone is a rollback, not a reset: see Where data lives.) |
| A broken venv | `make clean && make setup` |
| A broken agent worktree | `git worktree remove .claude/worktrees/<name>` |

Things that **can't** be undone from here: a force-push to `master` (prevented only by H2),
a leaked token (rotate it on GitHub), and money already spent.

### Budget: `make agent`

```sh
make agent TASK="Find why scooter 18 stays reserved; write a failing test" \
           BUDGET_USD=2 MAX_TURNS=40 TIMEOUT_MIN=20   # the defaults
make agent TASK="..." DRY_RUN=1            # print the exact claude command; no API call
scripts/agent.sh --help
```

- **The command it runs:** `claude -p "<task>" --max-budget-usd <B> --max-turns <N>
  --permission-mode acceptEdits --permission-prompts none --allowed-tools <make
  setup/test, read-only git, git add/commit>` (plus `--worktree <name>` if you set
  `WORKTREE`).
- **`WORKTREE` names:** letters, digits, `.`, `_` and `-`, starting with a letter or digit.
  `..`, a leading `.` and any `/` are rejected.
- **Effect:** edits inside the repo are accepted. Anything that would prompt is denied,
  because nobody is watching. That includes `git push` and `gh pr create`: the project
  allow rules for them don't apply in `-p` mode, and `agent.sh` deliberately doesn't
  re-grant them. It also includes unsandboxed retries and hosts outside the allowlist. A
  headless run therefore leaves its work as local commits for a normal session to push
  and review.
- **Wall clock:** a portable watchdog, needed because macOS has no `timeout(1)`. It sends
  TERM to the agent's whole process group after `TIMEOUT_MIN`, and KILL 15 s later. The
  script then exits with **124**. Limitation: a process the agent started in its *own*
  process group or session, such as a daemonised server or anything run via `setsid`,
  isn't in that group and may survive. Check `ps` after a timeout.
- **Turn cap:** `MAX_TURNS`, passed to `--max-turns`. That flag is **hidden from
  `claude --help`** but is supported: it's in the [CLI reference](https://code.claude.com/docs/en/cli-reference),
  and the 2.1.310 binary defines it as "Maximum number of agentic turns in non-interactive
  mode" (AI_NOTES #10).
- **Task text:** pass it through `TASK=`. With `make`, a `$` in the task is expanded by
  make, so write `$$` or call `scripts/agent.sh "<task>"` directly. A task can't start
  with `-`.
- **Verified without any API call:** `--help`, `DRY_RUN=1`, rejection of a bad budget,
  turn count, timeout, worktree name (including `..`, `.` and `a/b`) or task, and the
  watchdog. The watchdog was tested with a fake
  `claude` on `PATH`: a normal exit passed its status through, a hang gave 124 after 60 s,
  and a process ignoring TERM was KILLed and gave 124. No processes were left over.
  **A real paid run hasn't been made.**

### How to check the sandbox yourself

Start an interactive `claude` session in the repo root and run `/sandbox`. Its Config tab
shows the resolved policy. Then ask Claude (not `!`, which runs unsandboxed) to run each of
these:

- `touch ~/sandbox-probe` should fail with `Operation not permitted`.
- `curl --noproxy '*' https://example.com` should fail.
- `curl -sI https://example.com` should prompt, or be refused in `dontAsk`.
- `make test` should pass.

## Workflow rules

- **PLAN.md is the plan.** Work on one PLAN row per PR, with exactly that scope. If you find
  something else, report it or add it to the plan; don't fix it on the side. If the plan
  changes, edit PLAN.md in the same PR and add a Change log line.
- **Branch:** start from a fresh `master` (`git checkout master && git pull`), then
  `git checkout -b stage-<N>/<slug>`, where N is the PR number in PLAN.md's table. Push it
  with `git push -u origin stage-<N>/<slug>`; that shape is pre-approved. A slug must not
  contain `master`, because every push mentioning it is denied.
- **Never commit to or push `master`, never force-push, and never deploy.**
- **Merge only** after the reviewer's comment ends in `VERDICT: APPROVE` **and** CI is
  green. Use a squash merge (`gh pr merge <n> --squash --delete-branch --repo ...`).
  Merging is autonomous (see Agent boundaries → Escalation policy).
- **Escalate to the human only** when the docs don't settle a decision, or when the writer
  and reviewer disagree.
- **This repo is a fork.** Open PRs only against the fork, never the upstream
  `KaranErry/scooterAPI-reliability`:
  `gh pr create --repo Kiwi-223/scooterAPI-reliability-TI26 --base master ...`.
- **In each PR:** mark that PR's row ✅ in PLAN.md, keep this file accurate, and log any
  confident AI mistakes in `AI_NOTES.md`.
- **Commits** use conventional style (`build:`, `fix:`, `docs:`, ...).
- **Network to GitHub can be flaky** (TLS timeouts, connection resets). Re-run a failed
  `git push` or `gh` call, as a plain single command so it stays out of the sandbox (see
  Agent boundaries), rather than wrapping it in a shell retry loop. After a failed
  `gh pr create`, check
  `gh pr list --repo Kiwi-223/scooterAPI-reliability-TI26 --head <branch>` before you
  retry, to avoid opening duplicates.
- **Don't trust memory for library APIs or versions.** Check with `pip index versions`,
  the installed package source, or `--help`.
