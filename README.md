> **Tek Innov8rs Fall 2026:** this is a snapshot of a 2019 project, left as-is on purpose. Start with [ACTIVITY.md](ACTIVITY.md).

# Scooter Reservation API

## Introduction

### Stack

The implementation of this app uses Python/Flask, with JSON as the database. The 'geopy' library was used for accurately calculating the “crow flies” distance between two sets of coordinates. 

### Setup

#### On the web

The app is not currently deployed. The old Heroku URL from 2019 no longer works.

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

* **Code**: 422 <br />
  **Content**: JSON object with a `msg` property that gives a brief description of the error trigger, and an `error` object whose `kind` is a stable failure id (see [Errors](#errors)).

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

## Errors

Every error response, on every endpoint, is a JSON object:

```json
{"msg": "Error 422 - No scooter with id 42 was found.", "error": {"kind": "scooter_not_found"}}
```

`msg` is a human-readable description. It's unchanged from earlier versions of this API,
except that a non-numeric `radius` now says "Radius must be a number" instead of
"Lat/Lng values must be numbers". Match on `error.kind` instead. Besides the per-endpoint kinds listed above, any
endpoint can return `storage_error` or `internal_error`, and unknown paths or non-`GET`
methods return `route_not_found` or `method_not_allowed`. `error.kind` is one of the stable ids below. Ids are
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

