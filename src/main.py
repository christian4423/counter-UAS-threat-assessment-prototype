from pymavlink import mavutil
import math
import time
import shutil
import threading
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import matplotlib
matplotlib.use("Agg")  # non-interactive backend, just writes files
import matplotlib.pyplot as plt
from PIL import Image

# All output files land next to this script.
OUT_DIR = Path(__file__).resolve().parent
FRAMES_DIR = OUT_DIR / "threat_map_frames"

MAP_URL = "http://localhost:8080/map"
FRAME_INTERVAL = 2.0     # seconds between map frames
TRACK_INTERVAL = 1.0     # seconds between breadcrumbs kept in the track
TRACK_MAX_POINTS = 400   # caps the URL length of the track query param
GIF_SCALE = 0.75         # GIF frame size relative to the rendered map
GIF_FRAME_STEP = 2       # keep every Nth frame in the GIF
GIF_SPEEDUP = 8          # playback speed relative to real time

# Scripted "intruder" route around Little Caesars Arena (lat, lon). Starts at
# the SITL home (already inside the warning ring), runs out to clear airspace,
# then turns inbound, overflies the arena and exits NW before RTL brings it
# back across the no-fly zone to land.
CRUISE_ALT_M = 40
CRUISE_SPEED_MS = 12
WAYPOINT_RADIUS_M = 15
EARTH_RADIUS_M = 6371008.8
WAYPOINTS = [
    (42.3365, -83.0490),   # SE, outside warning ring  -> CLEAR
    (42.3395, -83.0510),   # inbound                   -> WARNING
    (42.3411, -83.0550),   # over the arena            -> BREACH
    (42.3450, -83.0605),   # exit NW                   -> CLEAR
]

# Create a connection to the SITL instance
# TCP 5760 is single-client only (the container's own MAVProxy already holds it),
# so we use the UDP stream MAVProxy forwards out, routed via host.docker.internal
# so it actually escapes the container's network namespace to reach the host.
master = mavutil.mavlink_connection('udp:127.0.0.1:14550')

# Wait for the heartbeat message to find the system ID
# Every compliant MAVLink system should send a heartbeat at a regular interval (1s)
# This is the only message that is guaranteed to be sent by all MAVLink systems
# payload of the heartbeat message contains information about the system's type, autopilot, and mode
# i.e. "here's who I am and roughly how I'm doing."
master.wait_heartbeat()
print("Heartbeat from system (system %u component %u)" % (master.target_system, master.target_component))

last_heartbeat = time.time()
HEARTBEAT_TIMEOUT = 5  # seconds without a heartbeat = consider link lost

# Dictionary to graph values of different message types
graph_values = defaultdict(list)

# Live vehicle state, updated from GLOBAL_POSITION_INT
position = None          # dict(lat, lon, alt, vn, ve)
track = []               # [(lat, lon), ...] breadcrumbs
last_track_time = 0.0
last_frame_time = 0.0

# Map frames are fetched on a worker thread so a slow render (tile download)
# never stalls the MAVLink receive loop.
frame_pool = ThreadPoolExecutor(max_workers=1)
frame_pending = None
frame_count = 0
threat_state = None
state_lock = threading.Lock()

def send_command(command, *params, wait_ack=True, safe_to_retry=True):
    last_sent = time.time()
    params = list(params) + [0] * (7 - len(params))

    def send_attempt(attempt):
        master.mav.command_long_send(
            master.target_system,
            master.target_component,
            command,
            attempt,
            *params,
        )

    attempt = 0
    send_attempt(attempt)
    if not wait_ack:
        return None
    # Pump other messages while waiting so telemetry keeps being recorded.
    deadline = time.time() + 5
    max_retry = 5
    while time.time() < deadline:
        # Only retry if it's safe to resend the command (idempotent or repeatable).
        # One-time commands (takeoff, etc.) don't retry; caller verifies success by state.
        if safe_to_retry and time.time() - last_sent > 1 and attempt < max_retry:
            attempt = attempt + 1
            last_sent = time.time()
            send_attempt(attempt)
        msg = pump()
        if msg and msg.get_type() == 'COMMAND_ACK' and msg.command == command:
            return mavutil.mavlink.enums['MAV_RESULT'][msg.result].name
    raise TimeoutError("Could not send command to vehicle")


def request_interval(msg_id, hz):
    # Request a data stream: MAV_CMD_SET_MESSAGE_INTERVAL asks the vehicle to actively
    # push the message at this rate instead of only replying when polled.
    result = send_command(mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, msg_id, int(1e6 / hz))
    print(f"Set message interval ({mavutil.mavlink.mavlink_map[msg_id].msgname} @ {hz}Hz): {result}")


def ground_distance_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def map_query(pos):
    params = {
        "lat": f"{pos['lat']:.7f}", "lon": f"{pos['lon']:.7f}",
        "vn": f"{pos['vn']:.2f}", "ve": f"{pos['ve']:.2f}", "alt": f"{pos['alt']:.1f}",
    }
    if track:
        params["track"] = ";".join(f"{la:.6f},{lo:.6f}" for la, lo in track[-TRACK_MAX_POINTS:])
    return f"{MAP_URL}?{urllib.parse.urlencode(params)}"


def fetch_map(url, outfile):
    """Render one map frame; logs threat-state transitions from the response headers."""
    global threat_state
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            outfile.write_bytes(resp.read())
            state = resp.headers.get("X-Threat-State")
            dist = resp.headers.get("X-Distance-M")
            ttb = resp.headers.get("X-Time-To-Breach-S")
    except Exception as e:
        print(f"Map render failed: {e}")
        return
    with state_lock:
        if state != threat_state:
            eta = f", no-fly in {float(ttb):.0f}s" if ttb else ""
            print(f"*** THREAT {threat_state or '-'} -> {state.upper()} ({float(dist):.0f} m{eta})")
            threat_state = state


def handle_position(msg):
    global position, last_track_time, last_frame_time, frame_pending, frame_count
    # GLOBAL_POSITION_INT: lat/lon in degE7, relative_alt in mm, vx/vy in cm/s (N/E)
    position = dict(lat=msg.lat / 1e7, lon=msg.lon / 1e7, alt=msg.relative_alt / 1000,
                    vn=msg.vx / 100, ve=msg.vy / 100)
    graph_values["GLOBAL_POSITION_INT.relative_alt_m"].append(position["alt"])
    graph_values["GLOBAL_POSITION_INT.ground_speed_ms"].append(math.hypot(position["vn"], position["ve"]))

    now = time.time()
    if now - last_track_time >= TRACK_INTERVAL:
        track.append((position["lat"], position["lon"]))
        last_track_time = now
    if now - last_frame_time >= FRAME_INTERVAL and (frame_pending is None or frame_pending.done()):
        frame_count += 1
        frame_pending = frame_pool.submit(
            fetch_map, map_query(position), FRAMES_DIR / f"frame_{frame_count:04d}.png")
        last_frame_time = now


def pump(timeout=1):
    """Read one telemetry message and record it. Every wait in the script goes
    through here, so graphs/track/map frames keep updating during each phase."""
    global last_heartbeat
    # Read the telemetry stream
    # loop and receive messages (.recv_match(...)),
    # see the typed, structured messages
    msg = master.recv_match(
        type=['HEARTBEAT', 'ATTITUDE', 'GLOBAL_POSITION_INT', 'COMMAND_ACK', 'STATUSTEXT'],
        blocking=True, timeout=timeout)
    if msg:
        msg_type = msg.get_type()
        if msg_type == 'HEARTBEAT':
            # GCS heartbeats (e.g. MAVProxy's own) also arrive here; only the
            # vehicle's count as proof the link is alive.
            if msg.get_srcSystem() == master.target_system:
                last_heartbeat = time.time()
        elif msg_type == 'STATUSTEXT':
            print(f"[vehicle] {msg.text}")
        elif msg_type == 'GLOBAL_POSITION_INT':
            handle_position(msg)
        elif msg_type == 'ATTITUDE':
            for key, value in msg.to_dict().items():
                if key == "mavpackettype":
                    continue
                # Namespace by message type so fields never collide across
                # message types (e.g. two types sharing a field name).
                graph_values[f"{msg_type}.{key}"].append(value)
    if time.time() - last_heartbeat > HEARTBEAT_TIMEOUT:
        raise ConnectionError("Heartbeat timeout! Link considered lost.")
    return msg


def wait_until(condition, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pump()
        if condition():
            return
    raise TimeoutError(f"Timed out waiting for {what}")


def set_mode(mode):
    master.set_mode(master.mode_mapping()[mode])
    wait_until(lambda: master.flightmode == mode, 10, f"{mode} mode")
    print(f"Mode: {mode}")


def arm():
    # Arm the vehicle: MAV_CMD_COMPONENT_ARM_DISARM, param1=1 to arm (0 to disarm).
    # ArduCopter rejects this until its pre-arm checks pass (GPS fix, EKF healthy),
    # which SITL usually satisfies within a few seconds of boot - so retry.
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            result = send_command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1)
        except TimeoutError:
            retry_at = time.time() + 3
            while time.time() < retry_at:
                pump()
            continue
        print(f"Arm command result: {result}")
        if result == "MAV_RESULT_ACCEPTED":
            wait_until(master.motors_armed, 5, "armed state")
            return
        retry_at = time.time() + 3
        while time.time() < retry_at:
            pump()
    raise TimeoutError("Vehicle never passed pre-arm checks")

def goto(lat, lon, alt):
    # Position-only setpoint in GUIDED mode; alt is relative to home.
    master.mav.set_position_target_global_int_send(
        0,                                                      # time_boot_ms
        master.target_system,                                   # target system ID
        master.target_component,                                # target component ID
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,      # coordinate frame
        0b0000111111111000,                                     # type_mask: use only lat/lon/alt
        int(lat * 1e7),                                         # latitude (scaled integer * 1e7)
        int(lon * 1e7),                                         # longitude (scaled integer * 1e7)
        alt,                                                    # altitude
        0, 0, 0,                                                # velocity
        0, 0, 0,                                                # acceleration
        0, 0                                                    # yaw / yaw rate
    )

def goto_until_reached(lat, lon, alt) -> bool:
    """
    Sends the global position setpoint at 2 Hz until the vehicle is inside 
    the specified WAYPOINT_RADIUS_M (in meters).
    """
    print(f"Navigating to Target: Lat={lat}, Lon={lon}, Alt={alt}")
    last_sent = 0
    deadline = time.time() + 180
    while time.time() < deadline:
        now = time.time()
        # 1. Send the command to the vehicle
        if now - last_sent > 0.5:
            goto(lat, lon, alt)
            last_sent = now
        pump()
        if ground_distance_m((position["lat"], position["lon"]), (lat, lon)) < WAYPOINT_RADIUS_M:
            print("Vehicle arrived at waypoint")
            return True
                
    raise TimeoutError("Vehicle did not reach waypoint in time")

def fly_mission():
    request_interval(mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE, 4)
    request_interval(mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 4)
    wait_until(lambda: position is not None, 10, "first position fix")

    set_mode("GUIDED")
    arm()

    print(f"Taking off to {CRUISE_ALT_M} m")
    send_command(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, CRUISE_ALT_M, safe_to_retry=False)
    wait_until(lambda: position["alt"] >= CRUISE_ALT_M * 0.95, 60, "takeoff altitude")

    # param1=1 ground speed, param2=speed m/s
    send_command(mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED, 1, CRUISE_SPEED_MS, -1)

    for i, (lat, lon) in enumerate(WAYPOINTS, 1):
        print(f"Waypoint {i}/{len(WAYPOINTS)}: {lat}, {lon}")
        goto_until_reached(lat, lon, CRUISE_ALT_M)

    set_mode("RTL")
    # RTL ends with an automatic landing + disarm.
    wait_until(lambda: not master.motors_armed(), 300, "landing and disarm")
    print("Landed and disarmed.")


def save_graphs(values, outfile=OUT_DIR / "telemetry_plot.png"):
    keys = [k for k, v in values.items() if v]
    if not keys:
        print("No data collected, skipping plot.")
        return
    fig, axes = plt.subplots(len(keys), 1, figsize=(8, 3 * len(keys)), squeeze=False)
    for ax, key in zip(axes[:, 0], keys):
        ax.plot(values[key])
        ax.set_title(key)
        ax.set_xlabel("Sample #")
    fig.tight_layout()
    fig.savefig(outfile)
    print(f"Saved plot to {outfile}")


def save_map_outputs():
    frame_pool.shutdown(wait=True)
    if position:
        fetch_map(map_query(position), OUT_DIR / "threat_map.png")
        print(f"Saved final map to {OUT_DIR / 'threat_map.png'}")
    # Full-res frames of satellite imagery make a ~80 MB GIF; downscaling,
    # dropping every other frame and a 128-colour palette gets it to ~5 MB.
    frames = sorted(FRAMES_DIR.glob("frame_*.png"))[::GIF_FRAME_STEP]
    if frames:
        images = []
        for f in frames:
            im = Image.open(f).convert("RGB")
            im = im.resize((int(im.width * GIF_SCALE), int(im.height * GIF_SCALE)), Image.LANCZOS)
            images.append(im.quantize(colors=128, method=Image.Quantize.MEDIANCUT,
                                      dither=Image.Dither.NONE))
        gif = OUT_DIR / "threat_map.gif"
        images[0].save(gif, save_all=True, append_images=images[1:], optimize=True, loop=0,
                       duration=int(FRAME_INTERVAL * GIF_FRAME_STEP * 1000 / GIF_SPEEDUP))
        print(f"Saved {len(frames)}-frame animation to {gif}")


# Start each run with a clean frame folder so the GIF only shows this flight.
shutil.rmtree(FRAMES_DIR, ignore_errors=True)
FRAMES_DIR.mkdir()

try:
    fly_mission()
except KeyboardInterrupt:
    print("Interrupted by user.")
except (ConnectionError, TimeoutError) as e:
    print(e)

save_graphs(graph_values)
save_map_outputs()
