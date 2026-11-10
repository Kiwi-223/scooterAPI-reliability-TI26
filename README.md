> **Tek Innov8rs Fall 2026:** this is a snapshot of a 2019 project, left as-is on purpose. Start with [ACTIVITY.md](ACTIVITY.md).

# Scooter Reservation API

## Introduction

### Stack

The implementation of this app uses Python/Flask, with JSON as the database. The 'geopy' library was used for accurately calculating the “crow flies” distance between two sets of coordinates. 

### Setup

#### On the web

The app is not currently deployed. The old Heroku URL from 2019 no longer works. See [Deploying](#deploying) for what a deploy would take, and what still blocks one.

#### On local dev environment

Requires Python 3.15 and `make`.

* Clone the repository
* Run `make setup` to create `.venv` and install the locked dependencies
* Run `make run` to start the app
* App will run on http://localhost:8080

See [AGENTS.md](AGENTS.md) for full onboarding: dependencies, data files, the repo map and workflow.

## API Endpoints

### View All Available Scooters

#### Endpoints:
```
GET /
GET /view_all_available
```

#### Parameters: 

None

#### Success Responses:

* **Code**: 200 <br />
  **Content**: JSON list of objects, with each object containing the following properties:

|     Key     | Data Type |                                      Description of Value                                      |
|:-----------:|:------------------:|:-------------------------------------------------------------------------------------------:|
|      id     |       string       |      The id of the scooter. This can be used to start and end reservations (see below).     |
|     lat     |   floating point   |                 Latitude coordinate of the current location of the scooter.                 |
|     lng     |   floating point   |                 Longitude coordinate of the current location of the scooter.                |
| is_reserved |       boolean      | Whether or not the scooter is currently reserved. Only unreserved scooters can be reserved. |


#### Error Responses:

This endpoint takes no parameters, so it has no 422 errors. Like every endpoint, it can return `storage_error` (500) if the database can't be read, or `internal_error` (500); see [Errors](#errors).

### Search for Scooters 

#### Endpoints:
```
GET /search
```

#### Parameters:

| Parameter |   Data Type Expected   |                                                          Description                                                          |
|:---------:|:----------------------:|:-----------------------------------------------------------------------------------------------------------------------------:|
|    lat    |     floating point     | Latitude coordinate of the desired location at which to reserve a scooter.                                                    |
|    lng    |     floating point     | Longitude coordinate of the desired location at which to reserve a scooter.                                                   |
|   radius  | integer/floating point | The radius of the search, i.e. The radius of the area around the desired location that is acceptable for reserving a scooter. In metres; must be a finite number ≥ 0 (`0` matches the exact location; there is no upper bound). |

#### Success Responses:

* **Code**: 200 <br />
  **Content**: JSON list of objects, with each object containing the following properties:

| Key | Data Type |                                  Description of Value                                 |
|:---:|:------------------:|:----------------------------------------------------------------------------------:|
|  id |       string       | The id of the scooter. This can be used to start and end reservations (see below). |
| lat |   floating point   |             Latitude coordinate of the current location of the scooter.            |
| lng |   floating point   |            Longitude coordinate of the current location of the scooter.            |


#### Error Responses:

* **Code**: 422 <br />
  **Content**: JSON object with a `msg` property that gives a brief description of the error trigger, and an `error` object whose `kind` is a stable failure id (see [Errors](#errors)).
  Kinds: `missing_param`, `invalid_number`, `invalid_coordinate`, `invalid_radius`.


### Reserve a Scooter

#### Endpoints:
```
GET /reservation/start
```

#### Parameters:

| Parameter | Data Type Expected |                                                       Description                                                      |
|:---------:|:------------------:|:----------------------------------------------------------------------------------------------------------------------:|
|     id    |       string       | The id of the scooter that is to be reserved. Go to _search_ or _view all available scooters_ to find out scooter IDs. |

#### Success Responses:

* **Code**: 200 <br />
  **Content**: JSON object with a `msg` property that gives a brief description of the successful operation, and a `reservation_id` property (string) that identifies the reservation just started. Each start gets a new `reservation_id`.
  ```json
  {"msg": "Scooter 18 was reserved successfully.", "reservation_id": "f36f087baa0842929afe8d9eaf0e8598"}
  ```
  `reservation_id` was added later; clients that only read `msg` are unaffected.


#### Error Responses:

* **Code**: 422 <br />
  **Content**: JSON object with a `msg` property that gives a brief description of the error trigger, and an `error` object whose `kind` is a stable failure id (see [Errors](#errors)).
  Kinds: `missing_param`, `scooter_not_found`, `scooter_already_reserved`, `scooter_payment_pending` (the scooter's last ride is still waiting for its payment to settle), and `storage_busy` (503, retryable).


### End Reservation for a Scooter

#### Endpoints:
```
GET /reservation/end
```

#### Parameters:

| Parameter | Data Type Expected |                                                            Description                                                            |
|:---------:|:------------------:|:---------------------------------------------------------------------------------------------------------------------------------:|
|     id    |       string       | The id of the scooter whose reservation is to be ended. The scooter must have been reserved first in order to end a reservation.  |
|    lat    |   floating point   | Latitude coordinate of the final location of the scooter where the trip is to be ended.                                           |
|    lng    |   floating point   | Longitude coordinate of the final location of the scooter where the trip is to be ended.                                          |

#### Success Responses:

* **Code**: 200 <br />
  **Content**: JSON object containing the following properties:

| Parameter | Original Data Type |                                                                                           Description                                                                                          |
|:---------:|:------------------:|:----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
|    msg    |       string       | Short message a brief description of the successful operation                                 |
| txn_id    | string             | The transaction ID / reference number associated with the successful payment transaction to be kept for the payee's records. |


#### Error Responses:

* **Code**: 422 <br />
  **Content**: JSON object with a `msg` property that gives a brief description of the error trigger, and an `error` object whose `kind` is a stable failure id (see [Errors](#errors)).
  Kinds: `missing_param`, `invalid_number`, `invalid_coordinate`, `scooter_not_found`, `no_active_reservation`, and `storage_busy` (503, retryable).

* **Codes**: 422, 504, 502, 409 <br />
  **Content**: the same envelope, with one of the payment kinds, plus a boolean `ride_ended`:
  - `payment_declined` (422): the payment was declined, but **the ride ended**: the scooter is free at the end location, nothing was charged, and the cost is recorded as an outstanding balance. The body adds `"ride_ended": true` and `"outstanding_balance": <amount>`. Don't retry.
  - `payment_timeout` (504) or `payment_failed` (502): the ride is **waiting for payment** (`"ride_ended": false`), and the scooter stays reserved. **Retry** `/reservation/end` to settle it. A retry is charged at most once, and it ends the ride where the **first** request asked, with that first amount, whatever `lat`/`lng` the retry sends.
  - `payment_conflict` (409): this reservation was already charged for a different ride; nothing was charged and the ride is still open (`"ride_ended": false`).

  The extra body fields are additive: clients that read only `msg` and `error.kind` are unaffected.

### Health Check

#### Endpoints:
```
GET /health
```

#### Parameters:

None

#### Success Responses:

* **Code**: 200 <br />
  **Content**: the database could be read and is valid. It never changes anything and never waits for a lock.
  ```json
  {"status": "ok", "db": {"readable": true, "scooters": 5, "reserved": 1, "payment_pending": 0, "unsupported": 0}}
  ```
  - `payment_pending` counts rides waiting for their payment to settle. If it stays above 0, reconcile isn't running (see [Deploying](#deploying)).
  - `unsupported` counts reservations in a payment state this server version doesn't handle (for example after rolling back to older code). Starting or ending those scooters fails with `storage_error`, so `status` is then `"degraded"`. It's still a 200, because every other scooter works and restarting the server wouldn't fix the data: a person has to look (AGENTS.md, Database schema).
  - Every call is written to the request records like any other request, so a frequent health probe grows the records file (see [Deploying](#deploying)).

#### Error Responses:

* **Code**: 500 <br />
  **Content**: the usual error envelope with `error.kind` `storage_error`: the database is missing, unreadable or corrupt. The `msg` names the cause (for example `(FileNotFoundError)`), never a file path.

## Errors

Every error response, on every endpoint, is a JSON object:

```json
{"msg": "Error 422 - No scooter with id 42 was found.", "error": {"kind": "scooter_not_found"}}
```

`msg` is a human-readable description. It's unchanged from earlier versions of this API,
except that a non-numeric `radius` now says "Radius must be a number" instead of
"Lat/Lng values must be numbers". Match on `error.kind` instead. Besides the per-endpoint kinds listed above, any
endpoint can return `storage_error` or `internal_error`, and unknown paths or non-`GET`
methods return `route_not_found` or `method_not_allowed`. A path with a doubled slash inside it (`/reservation//start`) is `route_not_found`, not a redirect. `error.kind` is one of the stable ids below. Ids are
never renamed; new ones may be added.

| `error.kind` | HTTP status | Meaning |
|---|---|---|
| `missing_param` | 422 | A required query parameter is missing. |
| `invalid_number` | 422 | `lat`, `lng` or `radius` isn't a number. |
| `invalid_radius` | 422 | `radius` is negative or not finite (`inf`, `nan`). |
| `invalid_coordinate` | 422 | Latitude outside [-90, 90] or longitude outside [-180, 180]. |
| `scooter_not_found` | 422 | No scooter has the given id. |
| `scooter_already_reserved` | 422 | The scooter is already reserved. |
| `no_active_reservation` | 422 | The scooter has no reservation to end. |
| `scooter_payment_pending` | 422 | The scooter's last ride is still waiting for payment to settle; it can't be reserved yet. |
| `payment_failed` | 502 | The payment gateway reported an error; the ride is waiting for payment. Retry ending it. |
| `payment_declined` | 422 | The payment was declined; the ride **ended** and its cost is recorded as an outstanding balance (`ride_ended: true`, `outstanding_balance`). |
| `payment_timeout` | 504 | The payment gateway timed out; the ride is waiting for payment. Retry ending it; it's never charged twice. |
| `payment_conflict` | 409 | This reservation was already charged for a different ride; nothing was charged and the ride is still open. |
| `route_not_found` | 404 | There's no endpoint at that path. |
| `method_not_allowed` | 405 | All endpoints are `GET`. |
| `storage_error` | 500 | The server couldn't read or write its database, or (start/end only) that scooter's reservation is in a state this server version can't handle. |
| `storage_busy` | 503 | The database was locked by another request for too long; nothing was changed. Retry after the `Retry-After` seconds (`/reservation/*` only). |
| `internal_error` | 500 | Unexpected server error. |


## Deploying

> **Not deployed, and not ready for real riders yet.** The app has the files a Heroku deploy
> needs (below), and a production-style run was smoke-tested locally. But the JSON-file
> database, ledger and records can't survive on Heroku's filesystem, and the payment
> gateway is a simulator. Both are blockers (human decision **H5** in [PLAN.md](PLAN.md)).

### What's in place

- **`Procfile`:**
  ```
  web: WEB_CONCURRENCY=${WEB_CONCURRENCY:-2} gunicorn --bind 0.0.0.0:$PORT --timeout 20 --graceful-timeout 25 --no-control-socket app:app
  ```
  - `--bind 0.0.0.0:$PORT`: Heroku routes to `$PORT` on any interface. This is explicit;
    gunicorn's own default is `127.0.0.1:8000`, or `0.0.0.0:$PORT` only when it notices `PORT`.
  - `WEB_CONCURRENCY=${WEB_CONCURRENCY:-2}`: the number of workers. gunicorn reads
    `WEB_CONCURRENCY` itself when it loads its config (`int(os.environ["WEB_CONCURRENCY"])`),
    so an **empty** value crashes it at startup, whatever `--workers` says. The prefix
    replaces an empty or unset value with 2 before gunicorn starts. Heroku sets
    `WEB_CONCURRENCY` per dyno size (2 on Eco/Basic/Standard-1X; not set on Fir), and
    `app.json` sets it to 2. Writes are serialised by the DB file lock, so more workers
    mainly help reads. `tests/test_deploy.py` runs this line with the variable unset,
    empty and `3`.
  - `--timeout 20`: a worker is killed after 20 s on one request. That must stay above the
    slowest legitimate request: `SCOOTER_DB_LOCK_TIMEOUT` (5 s) waiting for the lock, plus the
    gateway call made while holding it (20 ms simulated). Otherwise the client gets
    gunicorn's HTML 500 instead of a JSON `storage_busy`. It also stays below Heroku's
    30 s router timeout, after which the client has already been answered with an error.
  - **Under contention, the router can still answer first, in HTML.** Heroku's 30 s starts
    when the router hands the request to the dyno, so it includes time spent queued
    behind busy workers. Each sync worker serves one request at a time, and a
    reservation request can wait up to 5 s for the DB lock. With 2 workers and a burst of
    starts and ends, a request queued behind several of them can pass 30 s before any
    worker picks it up. The router then answers the client with its own **HTML 503**
    (error code H12 in the router log), not our JSON envelope, and no `X-Request-ID`
    record matches what the client saw. The dyno still processes the request
    afterwards, so its record shows what actually happened, possibly a start or end that
    succeeded. A client that gets a non-JSON 503 should check the scooter's state
    (`/view_all_available`) before retrying. Splitting the gateway call out of the lock
    (below) shortens the queue; more workers don't, because writers share one lock.
  - `--graceful-timeout 25`: on a restart, Heroku sends SIGTERM and then SIGKILL 30 s later.
    A request in progress gets up to 25 s to finish, so a ride isn't cut off mid-payment.
  - `--no-control-socket`: gunicorn 26 otherwise writes a control socket to
    `~/.gunicorn/gunicorn.ctl`.
  - `tests/test_deploy.py` checks these flags and the timeout arithmetic, and runs the line
    through gunicorn's `--print-config`.
- **`app.json`:** the name, a description, the `heroku/python` buildpack, **one** `web` dyno
  and safe env defaults (below).
- **`.python-version`:** `3.15`. Heroku supports Python 3.15 and recommends a major-only
  version (`3.15`), so each build gets the latest patch (checked against the Heroku Dev
  Center on 2026-11-10). Python is **not** a blocker.
- **`requirements.txt`:** the hash-locked runtime deps. Heroku installs only this file.
- **`/health`:** `GET /health` gives 200 when the DB is readable and valid, and
  `storage_error` (500) when it isn't. See [Health Check](#health-check).

### Blockers (H5): the data files on an ephemeral filesystem

The DB (`scooter_db.json`), the payments ledger (`data/payments_ledger.jsonl`) and the request
records (`data/records.jsonl`) are plain files. On Heroku:

- **Every restart wipes them.** Each dyno's filesystem is discarded when it restarts:
  - on every deploy (a new release);
  - at least once a day (Heroku cycles dynos every 24 hours, plus up to 216 random minutes).

  After a restart, the DB is back to the committed `scooter_db.json`, and the ledger and
  records are empty. So:
  - every ride in progress and every outstanding balance is lost;
  - the evidence for debugging is lost;
  - worst, the ledger, which is what stops a ride being charged twice, starts empty.
- **Why `legacy-33` comes back every time.** The committed `scooter_db.json` ships scooter
  33 **reserved**, in the legacy shape, so every restart restores a live ride with the
  fixed key `legacy-33` (AGENTS.md, Database schema). With the simulator and an empty
  ledger, ending it charges it again after every restart: one key, many charges. With a
  real gateway, which keeps its own idempotency cache, the second end under `legacy-33`
  would match the earlier charge (or conflict with it). The rider could then be told
  "paid" without paying, which is a free ride. **Deploy with a seed DB that has no live
  reservations** (every `is_reserved: false`, no `reservation` objects), so no fixed key
  ever reaches a gateway.
- **No sharing between dynos.** Each dyno has its own files, so two web dynos would be two
  separate databases. Run exactly one web dyno (`app.json` sets `quantity: 1`). Don't scale it.
- **Reconcile can't run from Heroku Scheduler.** Scheduled jobs run in one-off dynos, which
  have their own filesystem and can't see the web dyno's files.
- **Records have no rotation.** The file grows until the next restart wipes it, and on a
  persistent disk it grows forever. Every request adds one line (about 0.9 KB for
  `/health`, up to about 3 KB for an end), and that includes `/health`: a probe every
  10 s adds about 8,600 lines (about 8 MB) a day. Health calls
  are recorded deliberately, like every other request. A probe that fails (a corrupt DB)
  needs the same evidence as any other failure, and skipping or sampling one path would
  break "exactly one record per request", which the tools and tests rely on. Rotation
  (or shipping records to the log stream) is future work; until then, probe sparingly
  and move the file aside when it gets large.

Recommended persistence options. Picking one is the H5 decision:

1. **A managed database** (for example Heroku Postgres) for the scooters, outstanding balances
   and the ledger. A transaction replaces the file lock, "at most one charge per key" becomes
   a unique constraint, and reconcile can run from Scheduler. Records go to the log stream
   (stdout, kept by a log drain) or to a table. This is the most work: `app.py`'s storage
   layer and `payments.py`'s ledger are rewritten.
2. **A single instance with a persistent disk** (for example a small VM, or a platform with
   attached volumes). Point `SCOOTER_DB_PATH`, `SCOOTER_RECORDS_PATH` and
   `SCOOTER_PAYMENTS_LEDGER_PATH` at the volume and run reconcile from cron on the same
   machine. This needs no code change, but it's one instance only, and you need backups.
   (Restoring an old DB backup without its ledger is the rollback case in AGENTS.md.)

SQLite (H8's alternative) doesn't help on Heroku: it's still a file on the same
ephemeral disk.

### Other blockers and limits

- **The payment gateway is simulated** (`payments.py`). Nothing real is charged. A real
  gateway client has to replace `payments.charge`, and it must support idempotency keys.
- **The DB lock is held across the gateway call** (AGENTS.md, Payment state machine). That's
  safe with the simulator's 20 ms calls. A real gateway that takes seconds would make every
  other start or end wait, and they'd get `storage_busy` after `SCOOTER_DB_LOCK_TIMEOUT`.
  Before using a real gateway, split the end into three steps:
  1. save `payment_pending` under the lock;
  2. charge without holding the lock (the idempotency key already prevents a double charge);
  3. finish under the lock, re-checking the state, so a concurrent retry or reconcile can't
     finish the same ride twice.

  Then size gunicorn's `--timeout` above the lock wait plus the gateway's own timeout.
- **State-changing `GET`s and no authentication** (H4): kept for README compatibility. Anyone
  who can reach the app can start and end rides.
- **The `git_*` fields in request records are `null` on Heroku.** The deployed slug has no
  `.git`.
- **Request ids:** Heroku's router passes an `X-Request-ID` to the app (its log examples
  show 32 hex characters), and the app keeps any id of 1–64 `[A-Za-z0-9._-]`, so router
  logs and records share ids. Heroku also accepts client-supplied ids of up to 200
  characters containing `+`, `/` or `=`. The app replaces those (the record notes
  `client_value_rejected`), so the two ids differ for that request.

### Required environment

| Variable | Production value | Notes |
|---|---|---|
| `PORT` | set by Heroku | Never set it yourself. |
| `WEB_CONCURRENCY` | `2` (`app.json`) | gunicorn workers. An empty or unset value means 2 (the Procfile's prefix). |
| `ENABLE_FAULT_INJECTION` | **unset** | With `1`, any client could force payment faults with the `X-Payment-Fault` header. `app.json` doesn't set it. |
| `PAYMENT_FAULT` | `none` (`app.json`) | Any other value makes the simulator fail payments on purpose. |
| `SCOOTER_DB_LOCK_TIMEOUT` | `5` (`app.json`) | Keep it well below the Procfile's `--timeout 20`. |
| `RECONCILE_MAX_ATTEMPTS`, `RECONCILE_MAX_AGE_MINUTES` | `3`, `15` (`app.json`) | H3's terminal rule for stuck payments. |
| `SCOOTER_DB_PATH`, `SCOOTER_RECORDS_PATH`, `SCOOTER_PAYMENTS_LEDGER_PATH` | the defaults (`scooter_db.json`, `data/…`) | Relative to the app directory. With persistent storage (option 2), point all three at it. |
| `PAYMENT_SEED`, `PAYMENT_FAULT_RATE`, `PAYMENT_TIMEOUT_DELAY_MS` | unset | Simulator settings for tests. |

### Reconcile scheduling

`python -m reconcile` settles rides left `payment_pending` by a timeout or a gateway error
(AGENTS.md, Reconcile). Nothing runs it automatically: without it, those scooters stay
reserved until the rider retries. It must run **against the same files as the web
process**, every minute or so.
- With option 2, use a cron line on the same machine (AGENTS.md has one).
- With option 1, use Heroku Scheduler (every 10 minutes at most) or a `worker` process that
  loops.

Today on Heroku, neither works, because the files aren't shared. `/health`'s
`payment_pending` count is the signal that reconcile isn't running.

### Not part of the deployed app

- **The dev dependencies** (`requirements-dev.txt`: pytest, pip-tools, jsonschema and the
  `mcp` SDK) aren't installed. Heroku installs `requirements.txt` only.
- **The agent tooling** isn't run on Heroku:
  - `.mcp.json` and `mcp_server.py` (the agent-tools MCP server; it can't even import
    without `mcp`);
  - `.claude/`;
  - `scripts/agent.sh`;
  - `tests/`.

  These files are in the slug, but nothing runs them. A `.slugignore` could leave them out.

### Steps (once the blockers are resolved)

Documented only; none of this has been run.

```sh
heroku create <app-name>                  # or "Deploy to Heroku" with app.json
heroku config:set PAYMENT_FAULT=none WEB_CONCURRENCY=2   # app.json sets these on a button deploy
heroku config:unset ENABLE_FAULT_INJECTION
git push heroku master                    # builds with Python 3.15 and requirements.txt
heroku ps:scale web=1                     # exactly one web dyno (the data files are per dyno)
curl https://<app-name>.herokuapp.com/health
```

### Local production smoke test

Run the Procfile command against scratch files, with fault injection off. Bind to
`127.0.0.1` locally rather than every interface.

```sh
S=$(mktemp -d) && cp scooter_db.json "$S/db.json"
env -u ENABLE_FAULT_INJECTION PORT=8195 PAYMENT_FAULT=none WEB_CONCURRENCY=2 \
    SCOOTER_DB_PATH="$S/db.json" SCOOTER_RECORDS_PATH="$S/records.jsonl" \
    SCOOTER_PAYMENTS_LEDGER_PATH="$S/ledger.jsonl" \
    .venv/bin/gunicorn --bind 127.0.0.1:8195 --timeout 20 --graceful-timeout 25 --no-control-socket app:app
curl -s localhost:8195/health
curl -s -H 'X-Payment-Fault: decline' 'localhost:8195/reservation/end?id=33&lat=24.5&lng=-28'   # 200: header ignored
make records ARGS="--file $S/records.jsonl recent-failures"
```
