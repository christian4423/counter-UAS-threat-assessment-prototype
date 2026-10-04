import math
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
import mapscript

app = FastAPI()

MAP_FILE = "mapfiles/basemap.map"

# Protected asset: Little Caesars Arena, Detroit (lat, lon).
ASSET_NAME = "LITTLE CAESARS ARENA"
ASSET_POLYGON = [
    (42.340669910334114, -83.05357719991865),
    (42.342173077641434, -83.05460363538408),
    (42.34143981263954, -83.05641372273135),
    (42.33997104185913, -83.05522786995586),
]
ASSET_CENTER = (
    sum(p[0] for p in ASSET_POLYGON) / len(ASSET_POLYGON),
    sum(p[1] for p in ASSET_POLYGON) / len(ASSET_POLYGON),
)

# Airspace rings, measured from ASSET_CENTER.
NO_FLY_RADIUS_M = 200.0
WARNING_RADIUS_M = 500.0

# Default test position: the planned takeoff/landing spot.
DEFAULT_DRONE = (42.33927724437817, -83.05286810960271)

EARTH_RADIUS_M = 6371008.8
WGS84 = mapscript.projectionObj("init=epsg:4326")
WEB_MERCATOR = mapscript.projectionObj("init=epsg:3857")


def to_mercator(lat: float, lon: float) -> mapscript.pointObj:
    # GPS reports WGS84 (EPSG:4326); the map renders in EPSG:3857.
    point = mapscript.pointObj(lon, lat)
    point.project(WGS84, WEB_MERCATOR)
    return point


def ground_distance_m(a: tuple, b: tuple) -> float:
    """Haversine distance between two (lat, lon) pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def add_shape(layer: mapscript.layerObj, shape_type: int, points: list, text: str = None):
    shape = mapscript.shapeObj(shape_type)
    line = mapscript.lineObj()
    for p in points:
        line.add(p)
    shape.add(line)
    if text:
        shape.text = text
    layer.addFeature(shape)


def circle(center_lat: float, center_lon: float, radius_m: float, segments: int = 90) -> list:
    # Web Mercator stretches distances by 1/cos(lat), so scale the radius to
    # draw a true ground-distance circle.
    c = to_mercator(center_lat, center_lon)
    r = radius_m / math.cos(math.radians(center_lat))
    pts = [mapscript.pointObj(c.x + r * math.cos(2 * math.pi * i / segments),
                              c.y + r * math.sin(2 * math.pi * i / segments))
           for i in range(segments)]
    return pts + [pts[0]]


def parse_track(track: str) -> list:
    """'lat,lon;lat,lon;...' -> [(lat, lon), ...]"""
    try:
        return [tuple(float(v) for v in pair.split(",")) for pair in track.split(";") if pair]
    except ValueError:
        raise HTTPException(400, "track must be 'lat,lon;lat,lon;...'")


def time_to_breach_s(lat: float, lon: float, distance_m: float, vn: float, ve: float):
    """Seconds until the no-fly ring at the current closing speed, or None if
    already inside / not closing. Closing speed = velocity projected onto the
    unit vector pointing from the drone to the asset center."""
    if distance_m <= NO_FLY_RADIUS_M:
        return None
    dn = (ASSET_CENTER[0] - lat) * math.pi / 180 * EARTH_RADIUS_M
    de = (ASSET_CENTER[1] - lon) * math.pi / 180 * EARTH_RADIUS_M * math.cos(math.radians(lat))
    closing = (vn * dn + ve * de) / math.hypot(dn, de)
    if closing < 0.5:  # m/s; below this treat as loitering / moving away
        return None
    return (distance_m - NO_FLY_RADIUS_M) / closing


def threat_state(distance_m: float) -> str:
    if distance_m <= NO_FLY_RADIUS_M:
        return "breach"
    if distance_m <= WARNING_RADIUS_M:
        return "warning"
    return "clear"


@app.get("/map")
def get_map(
    lat: float = DEFAULT_DRONE[0],
    lon: float = DEFAULT_DRONE[1],
    track: Optional[str] = None,
    vn: float = 0.0,
    ve: float = 0.0,
    alt: Optional[float] = None,
    width: int = 800,
    height: int = 600,
    half_extent_m: float = 650.0,
):
    """Render the counter-UAS view: imagery, protected asset + airspace rings,
    the drone's track, and the drone marker colored by threat state.
    vn/ve = velocity north/east in m/s, alt = metres above home (both optional)."""
    mapobj = mapscript.mapObj(MAP_FILE)
    mapobj.setSize(width, height)

    # View stays centered on the protected asset, not the drone.
    center = to_mercator(*ASSET_CENTER)
    half = half_extent_m / math.cos(math.radians(ASSET_CENTER[0]))
    mapobj.setExtent(center.x - half, center.y - half, center.x + half, center.y + half)

    add_shape(mapobj.getLayerByName("zone_warning"), mapscript.MS_SHAPE_POLYGON,
              circle(*ASSET_CENTER, WARNING_RADIUS_M))
    add_shape(mapobj.getLayerByName("zone_nofly"), mapscript.MS_SHAPE_POLYGON,
              circle(*ASSET_CENTER, NO_FLY_RADIUS_M))
    add_shape(mapobj.getLayerByName("protected_asset"), mapscript.MS_SHAPE_POLYGON,
              [to_mercator(*p) for p in ASSET_POLYGON + ASSET_POLYGON[:1]])

    if track:
        fixes = parse_track(track) + [(lat, lon)]
        if len(fixes) >= 2:
            add_shape(mapobj.getLayerByName("track"), mapscript.MS_SHAPE_LINE,
                      [to_mercator(*f) for f in fixes])

    distance = ground_distance_m((lat, lon), ASSET_CENTER)
    state = threat_state(distance)
    ttb = time_to_breach_s(lat, lon, distance, vn, ve)
    marker_text = f"{distance:.0f} m" if alt is None else f"{distance:.0f} m / {alt:.0f} m AGL"
    add_shape(mapobj.getLayerByName(f"drone_{state}"), mapscript.MS_SHAPE_POINT,
              [to_mercator(lat, lon)], text=marker_text)

    banner = [ASSET_NAME, f"THREAT: {state.upper()} - {distance:.0f} m from center"]
    if state != "breach":
        banner.append("TIME TO NO-FLY: " + (f"{ttb:.0f} s" if ttb is not None else "not closing"))
    if vn or ve:
        banner.append(f"GROUND SPEED: {math.hypot(vn, ve):.1f} m/s")
    add_shape(mapobj.getLayerByName("status_banner"), mapscript.MS_SHAPE_POINT,
              [mapscript.pointObj(10, 10)], text="|".join(banner))

    image = mapobj.draw()
    return Response(content=image.getBytes(), media_type="image/png",
                    headers={"X-Threat-State": state, "X-Distance-M": f"{distance:.1f}",
                             "X-Time-To-Breach-S": f"{ttb:.1f}" if ttb is not None else ""})


@app.get("/health")
def health():
    return {"status": "ok"}
