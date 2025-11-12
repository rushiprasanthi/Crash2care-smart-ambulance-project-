# backend_extended.py
# Extended Flask + Flask-SocketIO server with multi-intersection and priority queues.
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, join_room
from geopy.distance import geodesic
import time

# Serve static files from ./static so the HTML can be opened at http://localhost:5000/
app = Flask(__name__, static_folder='static', static_url_path='')
socketio = SocketIO(app, cors_allowed_origins="*")

# Config
PRIORITY_RULES = {"pregnant": 10, "fever": 2}
STALE_SECONDS = 60
PREEMPT_MARGIN = 0
MAX_INTERSECTIONS = 100

# In-memory stores
intersections = {}  # intersection_id -> {id, name, lat, lon, range_m}
intersection_ambulances = {}  # intersection_id -> { ambulance_id -> entry }
intersection_state = {}  # intersection_id -> { active_top_id: str or None, last_emit_time: epoch }

# Helper functions
def compute_score(conditions):
    return sum(PRIORITY_RULES.get(c.lower(), 0) for c in (conditions or []))

def estimate_distance_m(lat1, lon1, lat2, lon2):
    return geodesic((lat1, lon1), (lat2, lon2)).meters

def estimate_eta_s(distance_m, speed_m_s):
    try:
        if speed_m_s and speed_m_s > 0:
            return distance_m / float(speed_m_s)
    except Exception:
        pass
    return None

def sorted_queue_for_intersection(iid):
    qmap = intersection_ambulances.get(iid, {})
    entries = list(qmap.values())
    # sort by: -score, eta (None -> inf), distance, timestamp
    def sort_key(e):
        eta = e.get("eta_s")
        return (-e.get("score", 0),
                eta if eta is not None else float("inf"),
                e.get("distance_m", float("inf")),
                e.get("timestamp", 0))
    entries.sort(key=sort_key)
    return entries

def emit_queue_update(iid):
    q = sorted_queue_for_intersection(iid)
    socketio.emit('priority_queue_update', {
        "intersection_id": iid,
        "queue": q,
        "timestamp": time.time()
    }, room=f"intersection:{iid}")

def decide_and_emit_signal(iid):
    state = intersection_state.setdefault(iid, {"active_top_id": None, "last_emit_time": 0})
    q = sorted_queue_for_intersection(iid)
    top = q[0] if q else None
    prev_top_id = state.get("active_top_id")
    prev_top_score = 0
    if prev_top_id:
        prev_entry = intersection_ambulances.get(iid, {}).get(prev_top_id)
        prev_top_score = prev_entry.get("score", 0) if prev_entry else 0

    if top:
        new_top_id = top["id"]
        new_top_score = top.get("score", 0)
        # Decide preemption or not
        preempt = False
        if prev_top_id is None:
            # No active -> promote top
            preempt = True
        elif new_top_id != prev_top_id and new_top_score >= prev_top_score + PREEMPT_MARGIN:
            preempt = True
        # Update state and emit
        if preempt:
            state["active_top_id"] = new_top_id
            state["last_emit_time"] = time.time()
            socketio.emit('signal_update', {
                "intersection_id": iid,
                "status": "EMERGENCY" if new_top_score > 0 else "NORMAL",
                "top_ambulance": top,
                "preempt": True,
                "message": f"Giving priority to {new_top_id}"
            }, room=f"intersection:{iid}")
        else:
            # No preempt but maybe send a heartbeat normal/EMERGENCY for top
            socketio.emit('signal_update', {
                "intersection_id": iid,
                "status": "EMERGENCY" if new_top_score > 0 else "NORMAL",
                "top_ambulance": top,
                "preempt": False
            }, room=f"intersection:{iid}")
    else:
        # queue empty — if there was an active top previously, emit CROSSED or TIMEOUT
        if prev_top_id is not None:
            state["active_top_id"] = None
            socketio.emit('signal_update', {
                "intersection_id": iid,
                "status": "CROSSED",
                "top_ambulance": None,
                "preempt": False,
                "message": "No ambulances in range; clearing priority"
            }, room=f"intersection:{iid}")
        else:
            # nothing to do; maybe emit normal
            socketio.emit('signal_update', {
                "intersection_id": iid,
                "status": "NORMAL",
                "top_ambulance": None,
                "preempt": False
            }, room=f"intersection:{iid}")

# Serve the frontend
@app.route('/')
def index():
    return app.send_static_file('trafficsignal.html')

# API endpoints
@app.route('/register_intersection', methods=['POST'])
def register_intersection():
    if len(intersections) >= MAX_INTERSECTIONS:
        return jsonify({"ok": False, "error": "Max intersections reached"}), 400
    j = request.get_json() or {}
    try:
        iid = str(j['id'])
        intersections[iid] = {
            "id": iid,
            "name": j.get('name', f"intersection:{iid}"),
            "lat": float(j['lat']),
            "lon": float(j['lon']),
            "range_m": float(j.get('range_m', 300.0))
        }
        intersection_ambulances.setdefault(iid, {})
        intersection_state.setdefault(iid, {"active_top_id": None, "last_emit_time": 0})
        return jsonify({"ok": True, "intersection": intersections[iid]}), 200
    except KeyError as ke:
        return jsonify({"ok": False, "error": f"Missing field {ke}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route('/intersections', methods=['GET'])
def list_intersections():
    return jsonify({"intersections": list(intersections.values())}), 200

@app.route('/set_priority_rules', methods=['POST'])
def set_priority_rules():
    j = request.get_json() or {}
    rules = j.get('rules')
    if not isinstance(rules, dict):
        return jsonify({"ok": False, "error": "rules must be an object/dict"}), 400
    # sanitize keys to lower-case and int/coerce values
    global PRIORITY_RULES
    new_rules = {}
    for k, v in rules.items():
        try:
            new_rules[str(k).lower()] = int(v)
        except Exception:
            return jsonify({"ok": False, "error": f"invalid rule value for {k}"}), 400
    PRIORITY_RULES = new_rules
    return jsonify({"ok": True, "rules": PRIORITY_RULES}), 200

@app.route('/update_location', methods=['POST'])
def update_location():
    """
    Backwards-compatible extended endpoint that:
    - computes scores
    - assigns ambulance to any intersections within their range
    - updates queues and emits events per intersection
    """
    try:
        j = request.get_json() or {}
        aid = str(j.get('id', 'default'))
        lat = float(j['lat'])
        lon = float(j['lon'])
        direction = j.get('direction')
        speed = j.get('speed_m_s')
        timestamp = j.get('timestamp', time.time())
        try:
            # accept ISO or epoch float
            timestamp = float(timestamp)
        except Exception:
            timestamp = time.time()
        patient_conditions = j.get('patient_conditions', []) or []
        patient_score_override = j.get('patient_score')
        if patient_score_override is not None:
            try:
                score = int(patient_score_override)
            except Exception:
                score = compute_score(patient_conditions)
        else:
            score = compute_score(patient_conditions)

        assigned = []
        # Evaluate across intersections (for production, pre-filter with spatial index)
        for iid, inter in intersections.items():
            # Use per-request override range if provided
            detection_range = float(j.get('range', inter.get('range_m', 300.0)))
            dist = estimate_distance_m(lat, lon, inter['lat'], inter['lon'])
            if dist <= detection_range:
                eta = estimate_eta_s(dist, speed) if speed is not None else None
                entry = {
                    "id": aid,
                    "lat": lat,
                    "lon": lon,
                    "direction": direction,
                    "speed_m_s": speed,
                    "timestamp": timestamp,
                    "patient_conditions": patient_conditions,
                    "score": score,
                    "distance_m": dist,
                    "eta_s": eta
                }
                # update or insert into intersection_ambulances
                amap = intersection_ambulances.setdefault(iid, {})
                amap[aid] = entry
                assigned.append(iid)
                # emit queue update
                emit_queue_update(iid)
                # decide whether to preempt and emit top
                decide_and_emit_signal(iid)

        return jsonify({"ok": True, "assigned_intersections": assigned}), 200

    except KeyError as ke:
        return jsonify({"ok": False, "error": f"Missing field: {ke}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# Background cleanup for stale ambulances
def cleanup_loop():
    while True:
        now = time.time()
        for iid in list(intersection_ambulances.keys()):
            amap = intersection_ambulances.get(iid, {})
            removed = False
            for aid, entry in list(amap.items()):
                if now - float(entry.get("timestamp", 0)) > STALE_SECONDS:
                    del amap[aid]
                    removed = True
            if removed:
                # emit updates and re-evaluate signals if we removed top or changed queue
                emit_queue_update(iid)
                decide_and_emit_signal(iid)
        time.sleep(5)

# Start background cleanup task
socketio.start_background_task(cleanup_loop)

# Socket handler: join room (clients call this via socket.emit on connect)
@socketio.on('join_intersection')
def on_join_intersection(data):
    iid = data.get('intersection_id')
    if not iid:
        return
    room = f"intersection:{iid}"
    join_room(room)
    # send current queue and state to new client
    emit_queue_update(iid)
    decide_and_emit_signal(iid)

if __name__ == "__main__":
    print("Smart Traffic Light Backend (extended) starting on http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)