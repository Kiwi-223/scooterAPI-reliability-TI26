"""The exact `msg` strings app.py returns today (README: 422 + JSON with `msg`)."""
MISSING_PARAMS = "Error 422 - Please include all required parameters in search query"
NOT_A_NUMBER = "Error 422 - Lat/Lng values must be numbers"
LAT_RANGE = "Error 422 - Latitude must be in the [-90, 90] range."
LNG_RANGE = "Error 422 - Longitude must be in the [-180, 180] range."


def reserved_ok(sid):
    return f"Scooter {sid} was reserved successfully."


def already_reserved(sid):
    return f"Error 422 - Scooter with id {sid} is already reserved."


def not_found(sid):
    return f"Error 422 - No scooter with id {sid} was found."


def ended_ok(sid):
    return f"Payment for scooter {sid} was made successfully and the reservation was ended."


def no_reservation(sid):
    return f"Error 422 - No reservation for scooter {sid} presently exists."
