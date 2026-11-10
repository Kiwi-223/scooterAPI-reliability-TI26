from flask import Flask, render_template, request, redirect, url_for, make_response
from geopy.distance import distance as geodesic
import json, os, werkzeug
from http import HTTPStatus
from werkzeug.exceptions import HTTPException

from failures import FailureKind, StorageError

app=Flask(__name__)

# Configuration from environment variables (unset or empty -> README default)
DEFAULT_DB_PATH = 'scooter_db.json'
DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 8080

def get_db_path():
	# Read on every call so a changed SCOOTER_DB_PATH takes effect without re-importing.
	# A relative path is resolved against the current working directory, as before.
	return os.environ.get('SCOOTER_DB_PATH') or DEFAULT_DB_PATH

# ==================
#  ERROR RESPONSES
# ==================

def error_response(kind, msg, status=None):
	# Every error response: {"msg": ..., "error": {"kind": <stable id>}} (see failures.py).
	body = { 'msg': msg, 'error': { 'kind': kind.value } }
	return json.dumps(body), status or kind.status, {'Content-Type':'application/json'}

@app.errorhandler(StorageError)
def handle_storage_error(e):
	# The log gets the DB path and the original exception (with its traceback); the
	# response stays path-free.
	app.logger.error('storage error: %s (db=%s)', e, e.path, exc_info=e.__cause__)
	return error_response(FailureKind.STORAGE_ERROR, f'Error 500 - {e}')

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
	return error_response(FailureKind.INTERNAL_ERROR, 'Error 500 - Internal server error.')

# root
@app.route('/')
def home():
	return redirect(url_for('view_all_available'))
	
@app.route('/view_all_available')
def view_all_available():
	db = init_db()
	available_scooters = [scooter for scooter in db if not scooter.is_reserved]
	available_scooters_dictlist = convert_db_to_dictlist(available_scooters)
	return json.dumps(available_scooters_dictlist), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# return a json-ified list of all the scooters with status 200


# Search for scooters
@app.route('/search', methods=['GET'])
def search():
	# Search for scooters in the database
	# parse request params
	try:
		search_lat, search_lng, search_radius = \
			float(request.args['lat']), \
			float(request.args['lng']), \
			float(request.args['radius'])	# parse request for search criteria
	except werkzeug.exceptions.BadRequestKeyError:
		# the required parameters are not present in the search query
		return error_response(FailureKind.MISSING_PARAM, 'Error 422 - Please include all required parameters in search query')
	except ValueError:
		return error_response(FailureKind.INVALID_NUMBER, 'Error 422 - Lat/Lng values must be numbers')

	# validate lat and long values
	if not -90 <= search_lat <= 90:
		return error_response(FailureKind.INVALID_COORDINATE, 'Error 422 - Latitude must be in the [-90, 90] range.')
	if not -180 <= search_lng <= 180:
		return error_response(FailureKind.INVALID_COORDINATE, 'Error 422 - Longitude must be in the [-180, 180] range.')
	
	db = init_db()	# initialize db

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

	db = init_db()
	
	# try and find the scooter with specified id
	scooter = get_scooter_with_id(reserve_id, db)
	if scooter:
		# reserve if possible
		if not scooter.is_reserved:
			# scooter can be reserved
			scooter.is_reserved = True
			write_db(db)	# update db
			success = { 'msg': f'Scooter {reserve_id} was reserved successfully.' }
			return json.dumps(success), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# respond with status 200

		else:
			# the scooter is already reserved
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
		end_lat, end_lng = \
			float(request.args['lat']), \
			float(request.args['lng'])
		db = init_db()
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
		
	# try and find the scooter with specified id
	scooter = get_scooter_with_id(scooter_id_to_end, db)
	if scooter:
		# end reservation if possible
		if scooter.is_reserved:
			# scooter is reserved and can be ended
			
			# initiate payment
			payment_response = pay(scooter, end_lat, end_lng)
			if payment_response['status']:
				# the payment was completed successfully
				
				# update scooter's reserved status and location
				scooter.is_reserved = False
				scooter.lat, scooter.lng = end_lat, end_lng
				write_db(db)
				# construct successful response
				success =	{	'msg': f'Payment for scooter {scooter_id_to_end} was made successfully and the reservation was ended.',
								'txn_id': payment_response['txn_id']
							}
				return json.dumps(success), HTTPStatus.OK.value, {'Content-Type':'application/json'}	# respond with status 200
			else:
				# the payment failed: the reservation is NOT ended (unchanged behaviour; PLAN PRs 13/14
				# define decline/timeout handling). Use the gateway's msg/code when it gives them.
				code = payment_response.get('code')
				if not (isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599):
					code = None  # a failed payment never carries a non-error status: fall back to 502
				return error_response(FailureKind.PAYMENT_FAILED,
					payment_response.get('msg', 'Error 502 - Payment failed; the reservation was not ended.'),
					code)
		else:
			# the scooter is not currently reserved
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
	# redirect to payment gateway and return response (currently a dummy function that returns a hypothetical transaction id)
	return payment_gateway(cost)	# returns hypothetical success and txn id for now
	
def payment_gateway(cost):
	# TODO: Implement real payment processing in future
	txn_id = 379892831
	return 	{	'status': True,
				'txn_id': txn_id
			}

def calculate_cost(distance):
	# TODO: Implement meaningful cost calculation in future
	return distance

		
def init_db():
	# Any problem reading the DB becomes a StorageError -> storage_error (500), never a
	# misleading validation error (a corrupt file raises ValueError, which end_reservation
	# would otherwise report as "Lat/Lng values must be numbers").
	path = get_db_path()
	try:
		with open(path, 'r') as db_file:
			db_json = db_file.read()
		db_list = json.loads(db_json)
	except OSError as e:
		raise StorageError(f'The scooter database could not be read ({type(e).__name__}).', path) from e
	except ValueError as e:
		raise StorageError(f'The scooter database is corrupt ({type(e).__name__}).', path) from e
	problem = find_db_shape_problem(db_list)
	if problem:
		raise StorageError(f'The scooter database is corrupt ({problem}).', path)
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
	
	
def get_scooter_with_id(search_id, db):
	try:
		scooter = next(scooter for scooter in db if scooter.id == search_id)	# get the scooter with specified id
		return scooter
	except StopIteration:
		# no scooter with the id was found
		return None
	

def write_db(db):
	# serialize Scooter objects 
	db_list = convert_db_to_dictlist(db)
	db_json = json.dumps(db_list)
	try:
		with open(get_db_path(), 'w') as db_file:
			db_file.write(db_json)
	except OSError as e:
		raise StorageError(f'The scooter database could not be written ({type(e).__name__}).', get_db_path()) from e
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