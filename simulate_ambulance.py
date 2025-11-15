"""
simulate_ambulance.py

Simple simulator that posts a moving ambulance to the backend `/update_location` endpoint
so you can test EMERGENCY -> CROSSED transitions and frontend behavior.

Usage (PowerShell):
  python simulate_ambulance.py --host http://localhost:5000 --id sim-1 --range 50

This script uses 'requests' (install via pip). It moves the simulated ambulance along a
straight line toward the traffic light, waits a bit inside range, then moves away.
"""
import time
import math
import argparse
import requests

# Traffic light location must match backend TRAFFIC_LIGHT_LOC
TRAFFIC_LIGHT_LAT = 16.5432
TRAFFIC_LIGHT_LON = 80.6123

# Convert meters to degrees latitude (approx). For small moves this is fine.
METERS_PER_DEG_LAT = 111320.0


def meters_to_lat_delta(meters):
    return meters / METERS_PER_DEG_LAT


def post_location(host, aid, lat, lon):
    url = host.rstrip('/') + '/update_location'
    payload = {'id': aid, 'lat': lat, 'lon': lon}
    try:
        r = requests.post(url, json=payload, timeout=5)
        try:
            print('POST', round(lat,6), round(lon,6), '->', r.status_code, r.json())
        except Exception:
            print('POST', round(lat,6), round(lon,6), '->', r.status_code, r.text)
    except Exception as e:
        print('POST failed:', e)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='http://localhost:5000', help='Backend base URL')
    p.add_argument('--id', default='sim-1', help='Ambulance id')
    p.add_argument('--range', type=float, default=50.0, help='Detection range in meters')
    p.add_argument('--approach', type=float, default=200.0, help='Start distance (m) from traffic light')
    p.add_argument('--speed', type=float, default=10.0, help='Approach speed in m/s')
    args = p.parse_args()

    host = args.host
    aid = args.id
    detection_range = args.range
    start_m = args.approach
    speed = args.speed

    # We'll move along latitude from start_m to -start_m (pass through the light)
    # Compute number of steps to reach the light at the given speed
    step_s = 1.0
    step_m = speed * step_s

    # Build sequence: approach to 0, stay inside for a few seconds, then leave
    positions = []
    m = start_m
    while m > 0:
        positions.append(m)
        m -= step_m
    # include a few positions inside range (0 and smaller)
    inside_steps = max(3, int(detection_range / step_m))
    for i in range(inside_steps):
        positions.append(max(0.0, -i * step_m / 2))
    # then move away
    m = step_m
    while m <= start_m:
        positions.append(m * -1)  # negative means passed the light in opposite direction
        m += step_m

    print('Simulating', len(positions), 'positions. Host:', host, 'id:', aid, 'range:', detection_range)

    for idx, meters in enumerate(positions):
        # position that is meters north of the traffic light (positive -> northwards)
        lat = TRAFFIC_LIGHT_LAT + meters_to_lat_delta(meters)
        lon = TRAFFIC_LIGHT_LON
        post_location(host, aid, lat, lon)
        time.sleep(step_s)

    print('Simulation complete')
