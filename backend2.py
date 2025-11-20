from flask import Flask, request, jsonify
from flask_socketio import SocketIO
from geopy.distance import geodesic

app = Flask(
    __name__,
    static_folder=r"C:\Users\Administrator\Desktop\ambulance\static",
    static_url_path=""
)
socketio = SocketIO(app, cors_allowed_origins="*")

# Default traffic light location (latitude, longitude)
TRAFFIC_LIGHT_LOC = (16.5432, 80.6123)  # example
# Default detection range in meters (you can change)
FIXED_DETECTION_RANGE = 50

# Track last in-range state per ambulance id (support multiple if needed)
_last_in_range = {}  # key: ambulance_id -> bool

@app.route('/')
def index():
    return app.send_static_file('trafficsignal1.html')

@app.route('/set_range', methods=['POST'])
def set_range():
    """
    Set the global detection range. Body: {"range": <meters>}
    """
    global FIXED_DETECTION_RANGE
    try:
        data = request.json or {}
        r = data.get('range')
        if r is None:
            return jsonify({"error": "Missing 'range' in JSON body"}), 400
        r = float(r)
        if r < 0:
            return jsonify({"error": "'range' must be non-negative"}), 400
        FIXED_DETECTION_RANGE = r
        socketio.emit('range_update', {'range': FIXED_DETECTION_RANGE})
        return jsonify({"message": f"Global range set to {FIXED_DETECTION_RANGE} meters"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/update_location', methods=['POST'])
def update_location():
    """
    Ambulance sends: { id: str (optional), lat: float, lon: float, direction: str (optional),
                       range: float (optional to override the global range for this request) }

    Emits socket events:
    - status: "EMERGENCY" -> ambulance entered detection range for the first time
    - status: "CROSSED"  -> ambulance left the detection range after being inside
    - status: "NORMAL"   -> ambulance is outside the detection range (and was already outside)
    """
    try:
        data = request.json or {}
        ambulance_id = data.get('id', 'default')
        lat = float(data['lat'])
        lon = float(data['lon'])
        ambulance_dir = data.get('direction', 'north')

        # Use per-request range if supplied, otherwise use global
        detection_range = float(data.get('range', FIXED_DETECTION_RANGE))

        ambulance_loc = (lat, lon)
        distance = geodesic(TRAFFIC_LIGHT_LOC, ambulance_loc).meters

        previously_in = _last_in_range.get(ambulance_id, False)
        currently_in = distance <= detection_range

        if currently_in and not previously_in:
            # Ambulance has just entered the controlled zone
            _last_in_range[ambulance_id] = True
            socketio.emit('signal_update', {
                'status': 'EMERGENCY',
                'direction': ambulance_dir,
                'distance_m': distance,
                'range_m': detection_range,
                'id': ambulance_id
            })
            return jsonify({"message": f"Ambulance entered detection range ({distance:.1f} m <= {detection_range} m)"}), 200

        if not currently_in and previously_in:
            # Ambulance has just left the controlled zone
            _last_in_range[ambulance_id] = False
            socketio.emit('signal_update', {
                'status': 'CROSSED',
                'direction': ambulance_dir,
                'distance_m': distance,
                'range_m': detection_range,
                'id': ambulance_id,
                'message': 'Ambulance successfully crossed the traffic system'
            })
            return jsonify({"message": f"Ambulance left detection range ({distance:.1f} m > {detection_range} m)"}), 200

        # No state change: either still inside or still outside
        if currently_in:
            # Optionally keep sending a heartbeat while inside
            socketio.emit('signal_update', {
                'status': 'EMERGENCY',
                'direction': ambulance_dir,
                'distance_m': distance,
                'range_m': detection_range,
                'id': ambulance_id
            })
            return jsonify({"message": "Ambulance still within detection range"}), 200
        else:
            socketio.emit('signal_update', {
                'status': 'NORMAL',
                'distance_m': distance,
                'range_m': detection_range,
                'id': ambulance_id
            })
            return jsonify({"message": "Ambulance outside detection range"}), 200

    except KeyError as ke:
        return jsonify({"error": f"Missing field: {ke}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# New: emit current range on new socket connection so UI can show it immediately
@socketio.on('connect')
def handle_connect():
    socketio.emit('range_update', {'range': FIXED_DETECTION_RANGE})

if __name__ == "__main__":
    print("🚦 Smart Traffic Light Backend Running at http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
