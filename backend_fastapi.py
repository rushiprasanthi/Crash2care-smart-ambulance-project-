"""
FastAPI + Socket.IO backend for Smart Traffic Light with ambulance preemption.

Run with:
  pip install -r requirements.txt
  uvicorn backend_fastapi:app --host 0.0.0.0 --port 8000

This server accepts POST /update_location and /set_range and emits Socket.IO events
('signal_update', 'range_update') to connected clients. Uses geodesic distance to
detect ambulance entering/leaving detection range and emits EMERGENCY/CROSSED/NORMAL.
"""
import os
import math
import time
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import socketio
from geopy.distance import geodesic

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Configuration
TRAFFIC_LIGHT_LOC = (16.5432, 80.6123)
FIXED_DETECTION_RANGE = float(os.environ.get('DETECTION_RANGE', '50.0'))
HYSTERESIS_METERS = float(os.environ.get('HYSTERESIS_METERS', '5.0'))

# Socket.IO async server and FastAPI app
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# Simple in-memory state
_last_in_range = {}
_is_emergency = {}


def calculate_bearing(pointA, pointB):
    """Return compass bearing in degrees from pointA -> pointB (lat, lon)."""
    lat1 = math.radians(pointA[0])
    lat2 = math.radians(pointB[0])
    diffLong = math.radians(pointB[1] - pointA[1])
    x = math.sin(diffLong) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(diffLong))
    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360
    return compass_bearing


def bearing_to_cardinal(bearing_deg):
    if (bearing_deg >= 315 and bearing_deg <= 360) or (bearing_deg >= 0 and bearing_deg < 45):
        return 'north'
    if bearing_deg >= 45 and bearing_deg < 135:
        return 'east'
    if bearing_deg >= 135 and bearing_deg < 225:
        return 'south'
    return 'west'


async def emit_signal(payload: dict):
    """Emit signal_update to all connected clients."""
    logging.info('Emitting signal_update %s', payload)
    await sio.emit('signal_update', payload)


@sio.event
async def connect(sid, environ):
    logging.info('Socket connected: %s', sid)
    # Send current detection range for UI
    await sio.emit('range_update', {'range': FIXED_DETECTION_RANGE}, to=sid)


@sio.event
async def disconnect(sid):
    logging.info('Socket disconnected: %s', sid)


@app.post('/set_range')
async def set_range(req: Request):
    global FIXED_DETECTION_RANGE
    data = await req.json()
    r = data.get('range')
    if r is None:
        raise HTTPException(status_code=400, detail="Missing 'range'")
    r = float(r)
    if r < 0:
        raise HTTPException(status_code=400, detail="Range must be non-negative")
    FIXED_DETECTION_RANGE = r
    logging.info('Set detection range -> %s m', FIXED_DETECTION_RANGE)
    await sio.emit('range_update', {'range': FIXED_DETECTION_RANGE})
    return JSONResponse({'message': f'Global range set to {FIXED_DETECTION_RANGE} meters'})


@app.post('/update_location')
async def update_location(req: Request):
    """Receive ambulance location updates from client devices.

    Expects JSON: { id: str, lat: float, lon: float, range?: float }
    Emits Socket.IO events: EMERGENCY / CROSSED / NORMAL
    """
    data = await req.json()
    ambulance_id = data.get('id', 'default')
    try:
        lat = float(data['lat'])
        lon = float(data['lon'])
    except Exception:
        raise HTTPException(status_code=400, detail='lat and lon required')

    detection_range = float(data.get('range', FIXED_DETECTION_RANGE))
    ambulance_loc = (lat, lon)
    distance = geodesic(TRAFFIC_LIGHT_LOC, ambulance_loc).meters

    try:
        bearing_deg = calculate_bearing(TRAFFIC_LIGHT_LOC, ambulance_loc)
        ambulance_dir = bearing_to_cardinal(bearing_deg)
    except Exception:
        ambulance_dir = 'north'
        bearing_deg = None

    previously_in = _last_in_range.get(ambulance_id, False)
    if previously_in:
        currently_in = distance <= (detection_range + HYSTERESIS_METERS)
    else:
        currently_in = distance <= detection_range

    # Entered
    if currently_in and not previously_in:
        _last_in_range[ambulance_id] = True
        _is_emergency[ambulance_id] = True
        payload = {
            'status': 'EMERGENCY',
            'direction': ambulance_dir,
            'bearing_deg': bearing_deg,
            'distance_m': round(distance, 1),
            'range_m': detection_range,
            'id': ambulance_id,
            'isEmergency': True,
            'ts': time.time()
        }
        await emit_signal(payload)
        return JSONResponse({'message': 'Ambulance entered detection range', 'payload': payload})

    # Left
    if not currently_in and previously_in:
        _last_in_range[ambulance_id] = False
        _is_emergency[ambulance_id] = False
        payload = {
            'status': 'CROSSED',
            'direction': ambulance_dir,
            'bearing_deg': bearing_deg,
            'distance_m': round(distance, 1),
            'range_m': detection_range,
            'stop_tracking': True,
            'id': ambulance_id,
            'message': 'Ambulance successfully crossed',
            'isEmergency': False,
            'ts': time.time()
        }
        await emit_signal(payload)
        return JSONResponse({'message': 'Ambulance left detection range', 'payload': payload})

    # Heartbeat while inside
    if currently_in:
        payload = {
            'status': 'EMERGENCY',
            'direction': ambulance_dir,
            'bearing_deg': bearing_deg,
            'distance_m': round(distance, 1),
            'range_m': detection_range,
            'id': ambulance_id,
            'isEmergency': True,
            'ts': time.time()
        }
        await emit_signal(payload)
        return JSONResponse({'message': 'Ambulance still inside range', 'payload': payload})

    # Outside
    payload = {
        'status': 'NORMAL',
        'distance_m': round(distance, 1),
        'range_m': detection_range,
        'id': ambulance_id,
        'isEmergency': False,
        'ts': time.time()
    }
    await emit_signal(payload)
    return JSONResponse({'message': 'Ambulance outside detection range', 'payload': payload})


@app.get('/')
async def index():
    # Serve a simple index if requested; the React UI is in static/react_trafficsignal.html
    html = (STATIC_HTML if (globals().get('STATIC_HTML')) else '<html><body>FastAPI Socket.IO backend</body></html>')
    return HTMLResponse(html)


if __name__ == '__main__':
    # When run directly use uvicorn
    import uvicorn
    port = int(os.environ.get('PORT', '8000'))
    logging.info('FastAPI Smart Traffic Light starting on port %s', port)
    uvicorn.run('backend_fastapi:asgi_app', host='0.0.0.0', port=port, reload=False)
