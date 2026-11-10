from contextlib import contextmanager
import copy, datetime, fcntl, tempfile, time, uuid
from flask import Flask, render_template, request, redirect, url_for, make_response
from geopy.distance import distance as geodesic
import json, math, os, werkzeug
from http import HTTPStatus
from werkzeug.exceptions import HTTPException

import payments
import records
from failures import FailureKind, StorageBusy, StorageError

app=Flask(__name__)
records.init_app(app)	# request ids + one JSON line per request (records.py, AGENTS.md)

# Configuration from environment variables (unset or empty -> README default)
DEFAULT_DB_PATH = 'scooter_db.json'
DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 8080

DEFAULT_DB_LOCK_TIMEOUT = 5.0	# seconds a mutating request waits for the DB lock

def get_db_path():
	# Read on every call so a changed SCOOTER_DB_PATH takes effect without re-importing.
	# A relative path is resolved against the current working directory, as before.
	return os.environ.get('SCOOTER_DB_PATH') or DEFAULT_DB_PATH

def get_db_lock_timeout():
	try:
		return max(0.0, float(os.environ.get('SCOOTER_DB_LOCK_TIMEOUT') or DEFAULT_DB_LOCK_TIMEOUT))
	except ValueError:
		return DEFAULT_DB_LOCK_TIMEOUT

# ==================
#  ERROR RESPONSES
# ==================

def error_response(kind, msg, status=None, extra=None):
	# Every error response: {"msg": ..., "error": {"kind": <stable id>}} (see failures.py).
	# `extra` adds top-level fields (payment failures add ride_ended / outstanding_balance, H7).
	body = { 'msg': msg, 'error': { 'kind': kind.value }, **(extra or {}) }
	records.failure(kind.value, msg)
	return json.dumps(body), status or kind.status, {'Content-Type':'application/json'}

@app.errorhandler(StorageError)
def handle_storage_error(e):
	# The log gets the DB path and the original exception (with its traceback); the
	# response stays path-free.
	app.logger.error('storage error: %s (db=%s)', e, e.path, exc_info=e.__cause__)
	records.exception(e.__cause__ or e)
	return error_response(FailureKind.STORAGE_ERROR, f'Error 500 - {e}')

@app.errorhandler(StorageBusy)
def handle_storage_busy(e):
	app.logger.warning('storage busy: %s (lock=%s, waited_ms=%s)', e, e.path, e.waited_ms)
	body, status, headers = error_response(FailureKind.STORAGE_BUSY, 'Error 503 - The scooter database is busy; nothing was changed. Retry shortly.')
	headers['Retry-After'] = '1'
	return body, status, headers

@app.errorhandler(HTTPException)
def handle_http_exception(e):
	# Framework errors (unknown route, wrong method, abort(...)) become JSON too.
	if e.code == HTTPStatus.NOT_FOUND:
		kind = FailureKind.ROUTE_NOT_FOUND
	elif e.code == HTTPStatus.METHOD_NOT_ALLOWED:
		kind = FailureKind.METHOD_NOT_ALLOWED
	else:
		# Includes abort(500) / InternalServerError, which is itself an HTTPException.
		kind = FailureKind.INTERNAL_ERROR
	body, _, headers = error_response(kind, f'Error {e.code} - {e.name}.')
	response = e.get_response()  # keeps e.code and headers such as Allow on a 405
	response.set_data(body)
	response.headers.update(headers)
	return response

@app.errorhandler(Exception)
def handle_unexpected_exception(e):
	# Registered for Exception (not 500) so it also runs when TESTING=True (AGENTS.md).
	# HTTPExceptions never reach here: the more specific handler above takes them.
	app.logger.exception('unhandled exception')
	records.exception(e)
	return error_response(FailureKind.INTERNAL_ERROR, 'Error 500 - Internal server error.')

# root
@app.route('/')
def home():
	return redirect(url_for('view_all_available'))
	
@app.route('/view_all_available')
def view_all_available():
	db = init_db(get_db_path())	# lock-free read: writers replace the file atomically
	available_scooters = [scooter for scooter in db if not scooter.is_reserved]
	records.note('filter', 'ok', available=len(available_scooters))
	available_scooters_dictlist = [scooter.to_public_dict() for scooter in available_scooters]
	return json.dumps(available_scooters_dictlist), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# return a json-ified list of all the scooters with status 200


# Search for scooters
@app.route('/search', methods=['GET'])
def search():
	# Search for scooters in the database
	# parse request params
	# Parsed in the same order as before (lat, lng, then radius), so which error wins when
	# several parameters are wrong is unchanged.
	try:
		search_lat, search_lng = \
			float(request.args['lat']), \
			float(request.args['lng'])
		raw_radius = request.args['radius']
	except werkzeug.exceptions.BadRequestKeyError:
		# the required parameters are not present in the search query
		return error_response(FailureKind.MISSING_PARAM, 'Error 422 - Please include all required parameters in search query')
	except ValueError:
		return error_response(FailureKind.INVALID_NUMBER, 'Error 422 - Lat/Lng values must be numbers')
	try:
		search_radius = float(raw_radius)
	except ValueError:
		return error_response(FailureKind.INVALID_NUMBER, 'Error 422 - Radius must be a number')

	# validate lat and long values
	if not -90 <= search_lat <= 90:
		return error_response(FailureKind.INVALID_COORDINATE, 'Error 422 - Latitude must be in the [-90, 90] range.')
	if not -180 <= search_lng <= 180:
		return error_response(FailureKind.INVALID_COORDINATE, 'Error 422 - Longitude must be in the [-180, 180] range.')
	# validate radius (metres): finite and >= 0. Zero is allowed (exact location). No upper
	# bound: any radius over ~20,004 km (half the Earth's circumference) simply covers every
	# scooter, which is a legitimate "everything" query and costs nothing extra.
	if not (math.isfinite(search_radius) and search_radius >= 0):
		return error_response(FailureKind.INVALID_RADIUS, 'Error 422 - Radius must be a finite number of metres that is 0 or more.')
	records.note('params', 'ok', lat=search_lat, lng=search_lng, radius_m=search_radius)
	
	db = init_db(get_db_path())	# lock-free read: writers replace the file atomically

	search_results = []
	for scooter in db:
		# Calculate distance between the scooter location point and the search location point, in metres
		distance = geodesic((scooter.lat, scooter.lng), (search_lat, search_lng)).m
		if distance <= search_radius and not scooter.is_reserved:
			# this scooter is available and within the search area
			search_results.append({	'id':scooter.id, 
									'lat':scooter.lat, 
									'lng':scooter.lng
								  })
			
	records.note('search', 'ok', matches=[r['id'] for r in search_results])
	return json.dumps(search_results), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# return json-ified search results list with status 200

	
# Start a reservation 
@app.route('/reservation/start', methods=['GET'])
def start_reservation():
	# parse request params
	try:
		reserve_id = request.args['id']	# parse request for id of scooter to be reserved
	except werkzeug.exceptions.BadRequestKeyError:
		# the required parameters are not present in the search query
		return error_response(FailureKind.MISSING_PARAM, 'Error 422 - Please include all required parameters in search query')
	records.scooter(scooter_id=reserve_id)

	# read, check and write under one exclusive lock (see db_transaction)
	with db_transaction() as txn:
		db = txn.db
		# try and find the scooter with specified id
		scooter = find_scooter(reserve_id, db)
		if scooter:
			refuse_unsupported_reservation(scooter, txn.path)
			# reserve if possible
			if not scooter.is_reserved:
				# scooter can be reserved
				records.note('reserve', 'allowed', reason='scooter is not reserved')
				scooter.reservation = new_reservation()
				scooter.is_reserved = True
				records.note('reservation', 'created', reservation_id=scooter.reservation['reservation_id'])
				write_db(db, txn.path)	# update db
				records.scooter(in_memory=scooter.to_dict())
				success = { 'msg': f'Scooter {reserve_id} was reserved successfully.',
							'reservation_id': scooter.reservation['reservation_id'] }
				return json.dumps(success), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# respond with status 200

			elif scooter.reservation['payment_state'] == 'payment_pending':
				# the last ride ended but its payment hasn't settled: not reservable yet
				records.note('reserve', 'refused', reason='the last ride is waiting for payment')
				records.scooter(in_memory=scooter.to_dict())
				records.unchanged()
				return error_response(FailureKind.SCOOTER_PAYMENT_PENDING, f'Error 422 - Scooter {reserve_id} cannot be reserved yet: its last ride is waiting for payment to settle.')
			else:
				# the scooter is already reserved
				records.note('reserve', 'refused', reason='scooter is already reserved')
				records.scooter(in_memory=scooter.to_dict())
				records.unchanged()
				return error_response(FailureKind.SCOOTER_ALREADY_RESERVED, f'Error 422 - Scooter with id {reserve_id} is already reserved.')
		else:
			# no scooter with the reserve id was found
			return error_response(FailureKind.SCOOTER_NOT_FOUND, f'Error 422 - No scooter with id {reserve_id} was found.')


# End a reservation
@app.route('/reservation/end', methods=['GET'])
def end_reservation():
	# parse request params
	try:
		scooter_id_to_end = request.args['id']	# parse request for id of scooter whose reservation to be ended
		records.scooter(scooter_id=scooter_id_to_end)
		end_lat, end_lng = \
			float(request.args['lat']), \
			float(request.args['lng'])
	except werkzeug.exceptions.BadRequestKeyError:
		# the required parameters are not present in the search query
		return error_response(FailureKind.MISSING_PARAM, 'Error 422 - Please include all required parameters in search query')
	except ValueError:
		return error_response(FailureKind.INVALID_NUMBER, 'Error 422 - Lat/Lng values must be numbers')

	# validate lat and long values
	if not -90 <= end_lat <= 90:
		return error_response(FailureKind.INVALID_COORDINATE, 'Error 422 - Latitude must be in the [-90, 90] range.')
	if not -180 <= end_lng <= 180:
		return error_response(FailureKind.INVALID_COORDINATE, 'Error 422 - Longitude must be in the [-180, 180] range.')
		
	records.note('params', 'ok', lat=end_lat, lng=end_lng)
	# All-or-nothing end (PLAN PR 14; AGENTS.md "Payment state machine"). Validate first, then
	# under one exclusive DB lock (see db_transaction):
	#   phase 1: persist payment_pending with the end location and amount BEFORE charging, so a
	#            crash or failed write after the charge leaves a ride that a retry can settle;
	#   charge:  at most once per idempotency key (payments.py's ledger);
	#   phase 2: persist the outcome (ended + paid, ended + outstanding balance, still pending,
	#            or back to active on a conflict).
	# The gateway call stays inside the lock: every transition of a scooter is serialised, so
	# no concurrent start/end can interleave with a half-finished payment. The cost is that a
	# slow gateway delays every writer (bounded here by PAYMENT_TIMEOUT_DELAY_MS).
	with db_transaction() as txn:
		db = txn.db
		# try and find the scooter with specified id
		scooter = find_scooter(scooter_id_to_end, db)
		if scooter:
			refuse_unsupported_reservation(scooter, txn.path)
			# end reservation if possible
			if scooter.is_reserved:
				if scooter.reservation['payment_state'] == 'active':
					# scooter is reserved and can be ended: phase 1
					records.note('end', 'allowed', reason='scooter is reserved')
					begin_payment(scooter, end_lat, end_lng)
					write_db(db, txn.path)
				else:
					# a retry of a ride whose payment is pending: it ends where it was first asked
					# to, for the stored amount; this request's coordinates are not used
					reservation = scooter.reservation
					records.note('end', 'retry', reason='payment is pending', end_lat=reservation['end_lat'],
						end_lng=reservation['end_lng'], amount=reservation['amount'], requested_lat=end_lat, requested_lng=end_lng)

				# charge (at most once per idempotency key), then phase 2
				payment_response = pay(scooter)
				outcome = payment_response.outcome
				if payment_response.ok or outcome == 'decline':
					# the ride ends: paid, or (H3) declined with an outstanding balance
					amount = scooter.reservation['amount']
					if outcome == 'decline':
						record_outstanding_balance(scooter)
					finish_ride(scooter)
					write_db(db, txn.path)
					records.scooter(in_memory=scooter.to_dict())
					if outcome == 'decline':
						return payment_failure_response(outcome, ride_ended=True, outstanding_balance=amount)
					# construct successful response
					success =	{	'msg': f'Payment for scooter {scooter_id_to_end} was made successfully and the reservation was ended.',
									'txn_id': payment_response.txn_id
								}
					return json.dumps(success), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# respond with status 200
				elif outcome == 'conflict':
					# the key's charge is for a different ride (e.g. a restored DB ended elsewhere):
					# nothing charged; back to an open ride, as before this request
					abandon_payment(scooter)
					write_db(db, txn.path)
					records.scooter(in_memory=scooter.to_dict())
					return payment_failure_response(outcome, ride_ended=False)
				else:
					# timeout or gateway error: the ride stays payment_pending until a retry (or
					# PLAN PR 15's reconcile) settles it
					note_payment_attempt(scooter, outcome)
					write_db(db, txn.path)
					records.scooter(in_memory=scooter.to_dict())
					return payment_failure_response(outcome, ride_ended=False)
			else:
				# the scooter is not currently reserved
				records.note('end', 'refused', reason='scooter is not reserved')
				records.scooter(in_memory=scooter.to_dict())
				records.unchanged()
				return error_response(FailureKind.NO_ACTIVE_RESERVATION, f'Error 422 - No reservation for scooter {scooter_id_to_end} presently exists.')
		else:
			# no scooter with the id was found
			return error_response(FailureKind.SCOOTER_NOT_FOUND, f'Error 422 - No scooter with id {scooter_id_to_end} was found.')


# ==================
#  HELPER FUNCTIONS	
# ==================


# ---- the end-ride payment state machine (AGENTS.md "Payment state machine") ------------
# Fields a reservation carries while payment_state is payment_pending (set by begin_payment).
PENDING_FIELDS = { 'end_lat', 'end_lng', 'distance_m', 'amount', 'payment_requested_at',
	'payment_request_id', 'payment_attempts', 'last_payment_outcome' }

def begin_payment(scooter, end_lat, end_lng):
	# active -> payment_pending: fix where the ride ends and what it costs, once. Retries and
	# reconcile reuse these values, so a retry from a slightly different spot is the same ride.
	distance_ridden = round(geodesic((scooter.lat, scooter.lng), (end_lat, end_lng)).m)	# metres
	cost = calculate_cost(distance_ridden)	# returns cost = distance for now
	scooter.reservation.update({	'payment_state': 'payment_pending',
									'end_lat': end_lat,
									'end_lng': end_lng,
									'distance_m': distance_ridden,
									'amount': cost,
									'payment_requested_at': utc_now_iso(),
									'payment_request_id': records.current_request_id(),
									'payment_attempts': 0,
									'last_payment_outcome': None
							   })
	records.note('payment', 'pending', end_lat=end_lat, end_lng=end_lng, distance_m=distance_ridden, amount=cost)

def pay(scooter):
	# One gateway attempt for a payment_pending ride, with its stored location and amount.
	reservation = scooter.reservation
	records.note('payment', 'requested', distance_m=reservation['distance_m'], cost=reservation['amount'],
		idempotency_key=reservation['idempotency_key'], end_lat=reservation['end_lat'], end_lng=reservation['end_lng'])
	# The simulated gateway (payments.py): at most one charge per idempotency key, and
	# reproducible faults (AGENTS.md "Payments & fault injection").
	override, override_note = payments.header_override(request.headers.get(payments.FAULT_HEADER))
	if override_note:
		records.note('payment_fault_override', override_note[0], **override_note[1])
	payment_response = payments.charge(reservation['idempotency_key'], scooter.id, reservation['reservation_id'],
		reservation['amount'], end_lat=reservation['end_lat'], end_lng=reservation['end_lng'],
		request_id=records.current_request_id(), override=override)
	records.note('payment', 'ok' if payment_response.ok else 'failed', **payment_response.record_detail())
	return payment_response

def note_payment_attempt(scooter, outcome):
	# payment_pending stays payment_pending; remember how many attempts failed and how.
	scooter.reservation['payment_attempts'] += 1
	scooter.reservation['last_payment_outcome'] = outcome
	records.note('reservation', 'payment_pending', reservation_id=scooter.reservation['reservation_id'],
		attempts=scooter.reservation['payment_attempts'], last_outcome=outcome)

def abandon_payment(scooter):
	# payment_pending -> active (a conflict): drop what begin_payment set; nothing was charged.
	for field in PENDING_FIELDS:
		scooter.reservation.pop(field, None)
	scooter.reservation['payment_state'] = 'active'
	records.note('reservation', 'payment_abandoned', reservation_id=scooter.reservation['reservation_id'],
		reason='the ledger has a charge for a different ride under this key')

def record_outstanding_balance(scooter):
	# H3: a definitive decline ends the ride without a charge; the debt is recorded in the DB,
	# in the same atomic write that ends the ride (AGENTS.md "Database schema").
	reservation = scooter.reservation
	balance = {	'reservation_id': reservation['reservation_id'],
				'idempotency_key': reservation['idempotency_key'],
				'amount': reservation['amount'],
				'end_lat': reservation['end_lat'],
				'end_lng': reservation['end_lng'],
				'ended_at': utc_now_iso(),
				'reason': 'payment_declined'
			  }
	scooter.outstanding_balances = (scooter.outstanding_balances or []) + [balance]
	records.note('balance', 'recorded', reservation_id=balance['reservation_id'], amount=balance['amount'])

def finish_ride(scooter):
	# payment_pending -> free: close the reservation and leave the scooter where the ride
	# ended (the location stored by begin_payment, not the current request's).
	reservation = scooter.reservation
	end_lat, end_lng = reservation['end_lat'], reservation['end_lng']
	records.note('reservation', 'cleared', reservation_id=reservation['reservation_id'],
		legacy=reservation.get('legacy', False))
	scooter.reservation = None
	scooter.is_reserved = False
	scooter.lat, scooter.lng = end_lat, end_lng

# How each payment outcome that doesn't simply pay for the ride is answered (H7).
PAYMENT_FAILURES = {
	'decline': (FailureKind.PAYMENT_DECLINED, 'Error 422 - The payment was declined. The ride was ended and its cost is recorded as an outstanding balance.'),
	'timeout_not_charged': (FailureKind.PAYMENT_TIMEOUT, 'Error 504 - The payment gateway timed out. The ride is waiting for payment; retry ending it to settle (it is charged at most once).'),
	'timeout_charged': (FailureKind.PAYMENT_TIMEOUT, 'Error 504 - The payment gateway timed out. The ride is waiting for payment; retry ending it to settle (it is charged at most once).'),
	'error': (FailureKind.PAYMENT_FAILED, 'Error 502 - Payment failed. The ride is waiting for payment; retry ending it to settle (it is charged at most once).'),
	'conflict': (FailureKind.PAYMENT_CONFLICT, 'Error 409 - This reservation already has a charge for a different ride; nothing was charged and the reservation was not ended.'),
}

def payment_failure_response(outcome, **extra):
	kind, msg = PAYMENT_FAILURES[outcome]
	return error_response(kind, msg, extra=extra)

def calculate_cost(distance):
	# TODO: Implement meaningful cost calculation in future
	return distance

		
# ==================
#  DB TRANSACTIONS
# ==================
#
# Concurrency model (AGENTS.md "Concurrency"):
# - Mutating requests run read -> check -> write inside db_transaction(), which holds an
#   exclusive flock on a sidecar lock file `<db>.lock` for the whole transaction. flock locks
#   belong to an open file description, so they exclude other threads (each opens its own
#   descriptor) as well as other processes / gunicorn workers.
# - The lock is on the sidecar, NOT on the DB file: write_db() replaces the DB with
#   os.replace(), which gives the path a new inode. A lock held on the old DB inode would not
#   stop a later request that opens the path (the new inode) and locks that, so two writers
#   could run at once. The sidecar's inode never changes.
# - Writes are atomic: temp file in the same directory, flush + fsync, os.replace over the
#   DB, fsync the directory. A crash mid-write leaves the old DB intact.
# - Reads (view, search) take no lock: open() sees either the old or the new complete file,
#   never a half-written one, so readers never wait on writers.
# - Waiting for the lock is bounded by SCOOTER_DB_LOCK_TIMEOUT (default 5 s); a timeout
#   raises StorageBusy -> storage_busy (503, Retry-After: 1) and nothing is read or written.

class DbTransaction:
	def __init__(self, path, db):
		self.path = path	# resolved once for the whole transaction
		self.db = db

def lock_path_for(db_path):
	return db_path + '.lock'

_LOCK_POLL_SECONDS = 0.005

def acquire_db_lock(db_path, timeout):
	# Returns the locked file descriptor; raises StorageBusy after `timeout` seconds.
	lock_path = lock_path_for(db_path)
	started = time.monotonic()
	try:
		fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
	except OSError as e:
		records.note('db_lock', 'failed', lock_path=lock_path, error=type(e).__name__)
		raise StorageError(f'The scooter database lock could not be opened ({type(e).__name__}).', lock_path) from e
	while True:
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			waited_ms = round((time.monotonic() - started) * 1000, 3)
			records.note('db_lock', 'acquired', lock_path=lock_path, wait_ms=waited_ms)
			return fd
		except BlockingIOError:
			waited = time.monotonic() - started
			if waited >= timeout:
				os.close(fd)
				waited_ms = round(waited * 1000, 3)
				records.note('db_lock', 'timeout', lock_path=lock_path, wait_ms=waited_ms, timeout_s=timeout)
				raise StorageBusy(f'The scooter database lock was not acquired within {timeout} s.', lock_path, waited_ms) from None
			time.sleep(_LOCK_POLL_SECONDS)

@contextmanager
def db_transaction():
	path = get_db_path()
	fd = acquire_db_lock(path, get_db_lock_timeout())
	try:
		yield DbTransaction(path, init_db(path))
	finally:
		fcntl.flock(fd, fcntl.LOCK_UN)
		os.close(fd)


def init_db(path=None):
	# Any problem reading the DB becomes a StorageError -> storage_error (500), never a
	# misleading validation error (a corrupt file raises ValueError, which end_reservation
	# would otherwise report as "Lat/Lng values must be numbers").
	path = path or get_db_path()
	try:
		with open(path, 'r') as db_file:
			db_json = db_file.read()
		db_list = json.loads(db_json)
	except OSError as e:
		records.note('db_read', 'failed', db_path=path, error=type(e).__name__)
		raise StorageError(f'The scooter database could not be read ({type(e).__name__}).', path) from e
	except ValueError as e:
		records.note('db_read', 'failed', db_path=path, error=type(e).__name__)
		raise StorageError(f'The scooter database is corrupt ({type(e).__name__}).', path) from e
	problem = find_db_shape_problem(db_list)
	if problem:
		records.note('db_read', 'failed', db_path=path, error=problem)
		raise StorageError(f'The scooter database is corrupt ({problem}).', path)
	records.note('db_read', 'ok', db_path=path, scooters=len(db_list))
	# populate Scooter objects for easier access later
	db = ScooterDb(scooter['id'] for scooter in db_list)
	for scooter in db_list:
		scooter_obj = Scooter(	scooter['id'], 
								scooter['lat'], 
								scooter['lng'], 
								scooter['is_reserved'],
								load_reservation(scooter),
								{ key: value for key, value in scooter.items() if key not in SCOOTER_FIELDS },
								scooter.get('outstanding_balances')
							 )
		db.append(scooter_obj)
	return db


DB_FIELDS = { 'id', 'lat', 'lng', 'is_reserved' }	# required in every entry
SCOOTER_FIELDS = DB_FIELDS | { 'reservation', 'outstanding_balances' }	# known; any other field is kept as-is (Scooter.extra)
# A reservation object (AGENTS.md "Database schema"). Unknown keys inside it are kept as-is.
RESERVATION_FIELDS = { 'reservation_id', 'started_at', 'start_request_id', 'payment_state', 'idempotency_key' }
# The payment states this code knows how to handle (AGENTS.md "Payment state machine"):
# `active` (riding) and `payment_pending` (ended, waiting for payment; PLAN PR 14). Later PRs
# may add more. A reservation in a state this code doesn't know still loads
# (the scooter counts as reserved, and the rest of the API keeps working), but start/end on
# that scooter are refused as storage_error rather than guessed at (ending it could charge
# twice), and write_db refuses to change it. See AGENTS.md "Database schema".
PAYMENT_STATES = { 'active', 'payment_pending' }
BALANCE_FIELDS = { 'reservation_id', 'idempotency_key', 'amount', 'end_lat', 'end_lng', 'ended_at', 'reason' }
LAT_RANGE, LNG_RANGE = (-90, 90), (-180, 180)

def _is_number(value):
	# bool is an int subclass in Python, so reject it explicitly; NaN/inf aren't coordinates.
	return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float('inf')

def _is_nonempty_str(value):
	return isinstance(value, str) and value != ''

def find_reservation_problem(reservation):
	# Returns a short, client-safe description of what's wrong with a reservation object, or None.
	if not isinstance(reservation, dict):
		return f'reservation is {type(reservation).__name__}, not an object or null'
	missing = RESERVATION_FIELDS - set(reservation)
	if missing:
		return f'reservation is missing {sorted(missing)}'
	for field in ('reservation_id', 'idempotency_key'):
		if not _is_nonempty_str(reservation[field]):
			return f'reservation.{field} is not a non-empty string'
	for field in ('started_at', 'start_request_id'):
		if reservation[field] is not None and not isinstance(reservation[field], str):
			return f'reservation.{field} is {type(reservation[field]).__name__}, not a string or null'
	if not _is_nonempty_str(reservation['payment_state']):
		return 'reservation.payment_state is not a non-empty string'
	if 'legacy' in reservation and not isinstance(reservation['legacy'], bool):
		return 'reservation.legacy is not a boolean'
	if reservation['payment_state'] == 'payment_pending':
		missing = PENDING_FIELDS - set(reservation)
		if missing:
			return f'reservation is payment_pending but missing {sorted(missing)}'
		problem = _find_location_amount_problem(reservation, 'reservation')
		if problem:
			return problem
		attempts = reservation['payment_attempts']
		if not (isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0):
			return 'reservation.payment_attempts is not a whole number >= 0'
		if reservation['last_payment_outcome'] is not None and not isinstance(reservation['last_payment_outcome'], str):
			return 'reservation.last_payment_outcome is not a string or null'
		for field in ('payment_requested_at', 'payment_request_id'):
			if reservation[field] is not None and not isinstance(reservation[field], str):
				return f'reservation.{field} is not a string or null'
	elif reservation['payment_state'] == 'active':
		present = PENDING_FIELDS & set(reservation)
		if present:
			return f'reservation is active but has payment fields {sorted(present)}'
	return None

def _find_location_amount_problem(obj, name):
	for field, (low, high) in (('end_lat', LAT_RANGE), ('end_lng', LNG_RANGE)):
		if not _is_number(obj[field]) or not low <= obj[field] <= high:
			return f'{name}.{field} is not a number in [{low}, {high}]'
	if not _is_number(obj['amount']) or obj['amount'] < 0:
		return f'{name}.amount is not a finite number >= 0'
	if 'distance_m' in obj and (not _is_number(obj['distance_m']) or obj['distance_m'] < 0):
		return f'{name}.distance_m is not a finite number >= 0'
	return None

def find_balances_problem(balances):
	if not isinstance(balances, list):
		return f'outstanding_balances is {type(balances).__name__}, not a list'
	for n, balance in enumerate(balances):
		if not isinstance(balance, dict):
			return f'outstanding_balances[{n}] is not an object'
		missing = BALANCE_FIELDS - set(balance)
		if missing:
			return f'outstanding_balances[{n}] is missing {sorted(missing)}'
		for field in ('reservation_id', 'idempotency_key', 'ended_at', 'reason'):
			if not _is_nonempty_str(balance[field]):
				return f'outstanding_balances[{n}].{field} is not a non-empty string'
		problem = _find_location_amount_problem(balance, f'outstanding_balances[{n}]')
		if problem:
			return problem
	return None

def find_db_shape_problem(db_list):
	# Returns a short, client-safe description of the first shape problem, or None.
	# Messages name entry indexes and fields, never values from the file.
	if not isinstance(db_list, list):
		return f'top level is {type(db_list).__name__}, not a list'
	first_index_of_id = {}
	# Reservation ids and idempotency keys must be unique across scooters too: two rides
	# sharing an idempotency key would be one charge for two rides (PLAN PR 13).
	first_index_of_key = { 'reservation_id': {}, 'idempotency_key': {} }
	for i, scooter in enumerate(db_list):
		if not isinstance(scooter, dict):
			return f'entry {i} is {type(scooter).__name__}, not an object'
		missing = DB_FIELDS - set(scooter)
		if missing:
			# Extra fields are tolerated (forward compatibility) and written back unchanged.
			return f'entry {i} is missing {sorted(missing)}'
		if not isinstance(scooter['id'], str):
			return f'entry {i}: id is {type(scooter["id"]).__name__}, not a string'
		if scooter['id'] in first_index_of_id:
			return f'entry {i}: duplicate id (same as entry {first_index_of_id[scooter["id"]]})'
		first_index_of_id[scooter['id']] = i
		for field, (low, high) in (('lat', LAT_RANGE), ('lng', LNG_RANGE)):
			if not _is_number(scooter[field]):
				return f'entry {i}: {field} is not a finite number'
			if not low <= scooter[field] <= high:
				return f'entry {i}: {field} is outside [{low}, {high}]'
		if not isinstance(scooter['is_reserved'], bool):
			return f'entry {i}: is_reserved is {type(scooter["is_reserved"]).__name__}, not a boolean'
		if 'reservation' in scooter:
			# The invariant: a scooter is reserved exactly when it has a reservation object.
			# (No 'reservation' key at all is the legacy, pre-PLAN PR 12 shape: see load_reservation.)
			reservation = scooter['reservation']
			if scooter['is_reserved'] and reservation is None:
				return f'entry {i}: is_reserved is true but reservation is null'
			if not scooter['is_reserved'] and reservation is not None:
				return f'entry {i}: is_reserved is false but reservation is set'
			if reservation is not None:
				problem = find_reservation_problem(reservation)
				if problem:
					return f'entry {i}: {problem}'
		if 'outstanding_balances' in scooter:
			problem = find_balances_problem(scooter['outstanding_balances'])
			if problem:
				return f'entry {i}: {problem}'
		reservation = load_reservation(scooter)	# legacy entries count with their synthesised ids
		if reservation is not None:
			for field, seen in first_index_of_key.items():
				if reservation[field] in seen:
					return f'entry {i}: duplicate reservation.{field} (same as entry {seen[reservation[field]]})'
				seen[reservation[field]] = i
	return None

def is_supported_reservation(reservation):
	# False for a reservation in a payment state this code doesn't handle (PAYMENT_STATES).
	return reservation is None or reservation['payment_state'] in PAYMENT_STATES

def refuse_unsupported_reservation(scooter, path):
	# start/end must not touch a reservation this code doesn't understand (see PAYMENT_STATES).
	if not is_supported_reservation(scooter.reservation):
		records.note('reservation', 'unsupported', reservation_id=scooter.reservation['reservation_id'],
			payment_state=scooter.reservation['payment_state'][:64])
		raise StorageError(f'The reservation for scooter {scooter.id} is in a payment state this server version '
			f'does not handle (reservation.payment_state is not one of {sorted(PAYMENT_STATES)}).', path)

def legacy_reservation(scooter_id):
	# A reservation for a scooter that was reserved before reservations were persisted
	# (PLAN PR 12): there is no record of when or by which request it started. Deterministic,
	# so every read of the same legacy entry gives the same reservation_id.
	reservation_id = f'legacy-{scooter_id}'
	return {	'reservation_id': reservation_id,
				'started_at': None,
				'start_request_id': None,
				'payment_state': 'active',
				'idempotency_key': reservation_id,
				'legacy': True
		   }

def load_reservation(scooter):
	# The reservation for a DB entry that passed find_db_shape_problem.
	if 'reservation' in scooter:
		return scooter['reservation']
	# Legacy entry (no 'reservation' key): free -> None; reserved -> a synthesised legacy
	# reservation. Either way the next write of the DB stores the new shape.
	return legacy_reservation(scooter['id']) if scooter['is_reserved'] else None

def utc_now_iso():
	return datetime.datetime.now(datetime.UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def new_reservation():
	# A reservation created by /reservation/start. idempotency_key equals reservation_id:
	# one reservation is one ride is one charge. It's stored as its own field so that a later
	# PR can derive it differently (e.g. per payment attempt) without changing the id.
	reservation_id = uuid.uuid4().hex
	return {	'reservation_id': reservation_id,
				'started_at': utc_now_iso(),
				'start_request_id': records.current_request_id(),
				'payment_state': 'active',
				'idempotency_key': reservation_id
		   }
	
	
def find_scooter(search_id, db):
	# get_scooter_with_id plus a record of the lookup and the scooter's state before the change
	scooter = get_scooter_with_id(search_id, db)
	if scooter:
		records.note('lookup', 'found', id=search_id)
		records.scooter(before=scooter.to_dict())
	else:
		records.note('lookup', 'not_found', id=search_id)
	return scooter


def get_scooter_with_id(search_id, db):
	try:
		scooter = next(scooter for scooter in db if scooter.id == search_id)	# get the scooter with specified id
		return scooter
	except StopIteration:
		# no scooter with the id was found
		return None
	

def _replace_atomically(path, data):
	# temp file in the same directory (so os.replace is a same-filesystem rename), fsync it,
	# rename it over the DB, then fsync the directory so the rename itself is durable.
	# Returns None, or the exception type name if only that final directory fsync failed:
	# by then the new data IS in place (visible to every reader), so that is not a failed
	# write; the caller logs a WARNING and records it, instead of reporting storage_error.
	if os.path.exists(path) and not os.access(path, os.W_OK):
		# A read-only DB means "don't write". os.replace only needs the directory to be
		# writable, so honour the file's permission explicitly.
		raise PermissionError(f'{path} is read-only')
	directory = os.path.dirname(os.path.abspath(path))
	fd, tmp_path = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=directory)
	try:
		with os.fdopen(fd, 'w') as tmp_file:
			tmp_file.write(data)
			tmp_file.flush()
			os.fsync(tmp_file.fileno())
		if os.path.exists(path):
			os.chmod(tmp_path, os.stat(path).st_mode & 0o7777)	# keep the DB's permissions
		os.replace(tmp_path, path)
	except BaseException:
		try:
			os.unlink(tmp_path)
		except OSError:
			pass
		raise
	try:
		dir_fd = os.open(directory, os.O_RDONLY)
		try:
			os.fsync(dir_fd)
		finally:
			os.close(dir_fd)
	except OSError as e:
		app.logger.warning('db written but directory fsync failed: %s (db=%s); the rename may not survive a power loss', e, path)
		return type(e).__name__
	return None


def write_db(db, path=None):
	# serialize Scooter objects 
	db_list = convert_db_to_dictlist(db)
	path = path or get_db_path()
	problem = find_db_shape_problem(db_list)
	loaded_ids = getattr(db, 'loaded_ids', None)
	if not problem and loaded_ids is None:
		problem = 'the scooters being written were not loaded by init_db (no loaded ids to compare)'
	if not problem:
		written_ids = { scooter['id'] for scooter in db_list }
		removed, added = sorted(loaded_ids - written_ids), sorted(written_ids - loaded_ids)
		if removed or added:
			problem = (f'the write would remove {len(removed)} and add {len(added)} scooter(s) '
				f'(removed ids {removed[:10]}, added ids {added[:10]})')
	for i, scooter in enumerate(db):
		# A reservation in a payment state this code doesn't handle is written back exactly as
		# it was read, never changed (and never created by this code).
		if not problem and scooter.reservation != scooter.loaded_reservation and not (
				is_supported_reservation(scooter.reservation) and is_supported_reservation(scooter.loaded_reservation)):
			problem = f'entry {i}: a reservation in an unknown payment state was changed'
	if problem:
		# A code bug produced state that init_db would refuse (e.g. reserved without a
		# reservation). Writing it would turn every later request into storage_error, so
		# refuse the write and leave the DB as it was; this request becomes internal_error.
		records.db_write(False, 'InvalidDbState')
		records.note('db_write', 'refused', db_path=path, error=problem)
		raise RuntimeError(f'refusing to write an invalid scooter database ({problem})')
	db_json = json.dumps(db_list)
	try:
		dir_fsync_error = _replace_atomically(path, db_json)
	except OSError as e:
		records.db_write(False, type(e).__name__)
		records.note('db_write', 'failed', db_path=path, error=type(e).__name__)
		raise StorageError(f'The scooter database could not be written ({type(e).__name__}).', path) from e
	records.db_write(True)
	records.written(db_list)	# scooter_after = what was actually serialised
	if dir_fsync_error:
		records.note('db_write', 'ok', db_path=path, dir_fsync_error=dir_fsync_error)
	else:
		records.note('db_write', 'ok', db_path=path)
	return True
		
class ScooterDb(list):
	# The list of Scooter objects init_db loaded, plus the ids it loaded. write_db refuses to
	# write a different set of ids: a write must never silently drop or invent a scooter.
	def __init__(self, loaded_ids):
		super().__init__()
		self.loaded_ids = frozenset(loaded_ids)

# class scooter for internal use
class Scooter:
	def __init__(self, scooter_id, lat, lng, is_reserved, reservation=None, extra=None, outstanding_balances=None):
		self.id = scooter_id
		self.lat = lat
		self.lng = lng
		self.is_reserved = is_reserved
		self.reservation = reservation	# dict while reserved, None while free (see RESERVATION_FIELDS)
		self.loaded_reservation = copy.deepcopy(reservation)	# as read from the DB (write_db's guard)
		self.extra = extra or {}		# unknown top-level DB fields, written back unchanged
		self.outstanding_balances = outstanding_balances	# None (absent) or a list (H3 declines)
	
	def to_dict(self):
		# The full persisted form: what write_db serialises and what request records show.
		scooter_dict = {	'id':self.id, 
							'lat':self.lat, 
							'lng':self.lng, 
							'is_reserved':self.is_reserved,
							'reservation':copy.deepcopy(self.reservation)
					   }
		if self.outstanding_balances is not None:
			scooter_dict['outstanding_balances'] = copy.deepcopy(self.outstanding_balances)
		for key, value in self.extra.items():
			scooter_dict[key] = copy.deepcopy(value)
		return scooter_dict

	def to_public_dict(self):
		# The README's public shape for /view_all_available: exactly these four fields.
		return { 'id':self.id, 'lat':self.lat, 'lng':self.lng, 'is_reserved':self.is_reserved }
		
def convert_db_to_dictlist(db):
	db_list = []
	for scooter in db:
		db_list.append(scooter.to_dict())
	return db_list
		


if __name__== "__main__":
	host = os.environ.get('HOST') or DEFAULT_HOST
	port = int(os.environ.get('PORT') or DEFAULT_PORT)
	app.run(host, port)	# debug stays off (Flask's default)