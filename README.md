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
|   radius  | integer/floating point | The radius of the search, i.e. The radius of the area around the desired location that is acceptable for reserving a scooter. |

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
  Kinds: `missing_param`, `invalid_number`, `invalid_coordinate`.


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
  **Content**: JSON object with a `msg` property that gives a brief description of the successful operation.


#### Error Responses:

* **Code**: 422 <br />
  **Content**: JSON object with a `msg` property that gives a brief description of the error trigger, and an `error` object whose `kind` is a stable failure id (see [Errors](#errors)).
  Kinds: `missing_param`, `scooter_not_found`, `scooter_already_reserved`.


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
  Kinds: `missing_param`, `invalid_number`, `invalid_coordinate`, `scooter_not_found`, `no_active_reservation`.

* **Code**: 502 (or the gateway's own code) <br />
  **Content**: the same envelope with kind `payment_failed`. The payment failed, the reservation was **not** ended and the scooter is still reserved.

## Errors

Every error response, on every endpoint, is a JSON object:

```json
{"msg": "Error 422 - No scooter with id 42 was found.", "error": {"kind": "scooter_not_found"}}
```

`msg` is a human-readable description. It's unchanged from earlier versions of this API,
so match on `error.kind` instead. Besides the per-endpoint kinds listed above, any
endpoint can return `storage_error` or `internal_error`, and unknown paths or non-`GET`
methods return `route_not_found` or `method_not_allowed`. `error.kind` is one of the stable ids below. Ids are
never renamed; new ones may be added.

| `error.kind` | HTTP status | Meaning |
|---|---|---|
| `missing_param` | 422 | A required query parameter is missing. |
| `invalid_number` | 422 | `lat`, `lng` or `radius` isn't a number. |
| `invalid_coordinate` | 422 | Latitude outside [-90, 90] or longitude outside [-180, 180]. |
| `scooter_not_found` | 422 | No scooter has the given id. |
| `scooter_already_reserved` | 422 | The scooter is already reserved. |
| `no_active_reservation` | 422 | The scooter has no reservation to end. |
| `payment_failed` | 502 | The payment failed; the reservation was not ended. |
| `route_not_found` | 404 | There's no endpoint at that path. |
| `method_not_allowed` | 405 | All endpoints are `GET`. |
| `storage_error` | 500 | The server couldn't read or write its database. |
| `internal_error` | 500 | Unexpected server error. |

