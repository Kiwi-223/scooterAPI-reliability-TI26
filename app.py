from contextlib import contextmanager
import fcntl, tempfile, time
from flask import Flask, render_template, request, redirect, url_for, make_response
from geopy.distance import distance as geodesic
import json, math, os, werkzeug
from http import HTTPStatus
from werkzeug.exceptions import HTTPException

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

def error_response(kind, msg, status=None):
	# Every error response: {"msg": ..., "error": {"kind": <stable id>}} (see failures.py).
	body = { 'msg': msg, 'error': { 'kind': kind.value } }
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
	available_scooters_dictlist = convert_db_to_dictlist(available_scooters)
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
			# reserve if possible
			if not scooter.is_reserved:
				# scooter can be reserved
				records.note('reserve', 'allowed', reason='scooter is not reserved')
				scooter.is_reserved = True
				write_db(db, txn.path)	# update db
				records.scooter(in_memory=scooter.to_dict())
				success = { 'msg': f'Scooter {reserve_id} was reserved successfully.' }
				return json.dumps(success), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# respond with status 200

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
	# Validate first, then read, check, pay and write under one exclusive lock (see
	# db_transaction). NOTE for PLAN PRs 13/14: the payment call happens while the lock is
	# held, so a slow gateway delays every other writer; those PRs persist payment_pending
	# and can release the lock around the gateway call.
	with db_transaction() as txn:
		db = txn.db
		# try and find the scooter with specified id
		scooter = find_scooter(scooter_id_to_end, db)
		if scooter:
			# end reservation if possible
			if scooter.is_reserved:
				# scooter is reserved and can be ended
				records.note('end', 'allowed', reason='scooter is reserved')
			
				# initiate payment
				payment_response = pay(scooter, end_lat, end_lng)
				if payment_response['status']:
					# the payment was completed successfully
				
					# update scooter's reserved status and location
					scooter.is_reserved = False
					scooter.lat, scooter.lng = end_lat, end_lng
					write_db(db, txn.path)
					records.scooter(in_memory=scooter.to_dict())
					# construct successful response
					success =	{	'msg': f'Payment for scooter {scooter_id_to_end} was made successfully and the reservation was ended.',
									'txn_id': payment_response['txn_id']
								}
					return json.dumps(success), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# respond with status 200
				else:
					# the payment failed: the reservation is NOT ended (unchanged behaviour; PLAN PRs 13/14
					# define decline/timeout handling). Use the gateway's msg/code when it gives them.
					records.scooter(in_memory=scooter.to_dict())	# expected: still reserved, not moved
					records.unchanged()
					code = payment_response.get('code')
					if not (isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599):
						code = None  # a failed payment never carries a non-error status: fall back to 502
					return error_response(FailureKind.PAYMENT_FAILED,
						payment_response.get('msg', 'Error 502 - Payment failed; the reservation was not ended.'),
						code)
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


def pay(scooter, end_lat, end_lng):
	# Initialise the payment process
	# construct location point tuples
	old_location = (scooter.lat, scooter.lng)
	new_location = (end_lat, end_lng)
	# calculate distance between points, in metres
	distance_ridden = geodesic(old_location, new_location).m
	distance_ridden = round(distance_ridden)
	# calculate cost (currently a dummy function that returns the distance as the cost)
	cost = calculate_cost(distance_ridden)	# returns cost = distance for now
	records.note('payment', 'requested', distance_m=distance_ridden, cost=cost)
	# redirect to payment gateway and return response (currently a dummy function that returns a hypothetical transaction id)
	payment_response = payment_gateway(cost)	# returns hypothetical success and txn id for now
	records.note('payment', 'ok' if payment_response.get('status') else 'failed',
		txn_id=payment_response.get('txn_id'), gateway_msg=payment_response.get('msg'), gateway_code=payment_response.get('code'))
	return payment_response
	
def payment_gateway(cost):
	# TODO: Implement real payment processing in future
	txn_id = 379892831
	return 	{	'status': True,
				'txn_id': txn_id
			}

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
	db = []
	for scooter in db_list:
		scooter_obj = Scooter(	scooter['id'], 
								scooter['lat'], 
								scooter['lng'], 
								scooter['is_reserved']
							 )
		db.append(scooter_obj)
	return db


DB_FIELDS = { 'id', 'lat', 'lng', 'is_reserved' }

def _is_number(value):
	# bool is an int subclass in Python, so reject it explicitly; NaN/inf aren't coordinates.
	return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float('inf')

def find_db_shape_problem(db_list):
	# Returns a short, client-safe description of the first shape problem, or None.
	if not isinstance(db_list, list):
		return f'top level is {type(db_list).__name__}, not a list'
	for i, scooter in enumerate(db_list):
		if not isinstance(scooter, dict):
			return f'entry {i} is {type(scooter).__name__}, not an object'
		missing = DB_FIELDS - set(scooter)
		if missing:
			# Extra fields are tolerated (forward compatibility, e.g. PLAN PR 12's reservation state).
			return f'entry {i} is missing {sorted(missing)}'
		if not isinstance(scooter['id'], str):
			return f'entry {i}: id is {type(scooter["id"]).__name__}, not a string'
		for field in ('lat', 'lng'):
			if not _is_number(scooter[field]):
				return f'entry {i}: {field} is not a finite number'
		if not isinstance(scooter['is_reserved'], bool):
			return f'entry {i}: is_reserved is {type(scooter["is_reserved"]).__name__}, not a boolean'
	return None
	
	
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
	dir_fd = os.open(directory, os.O_RDONLY)
	try:
		os.fsync(dir_fd)
	finally:
		os.close(dir_fd)


def write_db(db, path=None):
	# serialize Scooter objects 
	db_list = convert_db_to_dictlist(db)
	db_json = json.dumps(db_list)
	path = path or get_db_path()
	try:
		_replace_atomically(path, db_json)
	except OSError as e:
		records.db_write(False, type(e).__name__)
		records.note('db_write', 'failed', db_path=path, error=type(e).__name__)
		raise StorageError(f'The scooter database could not be written ({type(e).__name__}).', path) from e
	records.db_write(True)
	records.written(db_list)	# scooter_after = what was actually serialised
	records.note('db_write', 'ok', db_path=path)
	return True
		
# class scooter for internal use
class Scooter:
	def __init__(self, scooter_id, lat, lng, is_reserved):
		self.id = scooter_id
		self.lat = lat
		self.lng = lng
		self.is_reserved = is_reserved
	
	def to_dict(self):
		return {	'id':self.id, 
					'lat':self.lat, 
					'lng':self.lng, 
					'is_reserved':self.is_reserved
			   }
		
def convert_db_to_dictlist(db):
	db_list = []
	for scooter in db:
		db_list.append(scooter.to_dict())
	return db_list
		


if __name__== "__main__":
	host = os.environ.get('HOST') or DEFAULT_HOST
	port = int(os.environ.get('PORT') or DEFAULT_PORT)
	app.run(host, port)	# debug stays off (Flask's default)