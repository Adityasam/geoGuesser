import io
import math
import os
import random
import sqlite3
import threading
import time
import zipfile
from collections import deque
from functools import lru_cache

import mapbox_vector_tile
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "cities.db")
GEONAMES = "https://download.geonames.org/export/dump/cities15000.zip"

TOKEN = os.environ.get("MAPILLARY_TOKEN", "")
TILES = "https://tiles.mapillary.com/maps/vtp/mly1_public/2"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
UA = "geoguesser-game/0.1"  # Nominatim rejects requests without a real User-Agent
ZOOM = 14  # the only zoom where Mapillary's public tiles carry the image layer

MIN_POP = 50_000  # below this, Mapillary coverage gets thin enough to waste fetches
MIN_PANOS = 10    # a tile with 1 lonely image makes for a repetitive city
MAX_SCORE = 2_000  # points for a perfect guess
# Exponential falloff: every FALLOFF_KM of error cuts the score to ~37% of it.
# 1500 km is GeoGuessr's constant (5000 * exp(-10d / 14916 km map diagonal)).
# Lower it to punish near misses harder; raise it to be more forgiving.
FALLOFF_KM = 1500
WARM = deque(maxlen=24)  # cities whose tile is cached and known to hold 360s

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(16))


# --------------------------------------------------------------------------- db


def build_db():
    """Download GeoNames cities15000 into a local SQLite table. Runs once."""
    r = requests.get(GEONAMES, timeout=300)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        lines = z.read("cities15000.txt").decode("utf-8").splitlines()

    rows = []
    for line in lines:
        f = line.split("\t")
        rows.append((int(f[0]), f[1], f[8], float(f[4]), float(f[5]), int(f[14] or 0)))

    with sqlite3.connect(DB) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS cities(
                 id INTEGER PRIMARY KEY, name TEXT, country TEXT,
                 lat REAL, lon REAL, population INTEGER,
                 pano INTEGER)"""  # NULL = never checked, 0 = no 360s, 1 = has them
        )
        db.executemany(
            "INSERT OR IGNORE INTO cities(id,name,country,lat,lon,population)"
            " VALUES(?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def pick_city():
    """A random (id, lat, lon), skipping cities already known to have no 360s.

    ponytail: population-weighted via `uniform / population`, taking the
    minimum -- a one-expression weighted sample. Big cities come up more often
    because they're likelier to have coverage, so fewer wasted tile fetches.
    """
    with sqlite3.connect(DB) as db:
        return db.execute(
            """SELECT id, lat, lon FROM cities
               WHERE population >= ? AND pano IS NOT 0
               ORDER BY (ABS(RANDOM()) % 1000000) / CAST(population AS REAL)
               LIMIT 1""",
            (MIN_POP,),
        ).fetchone()


def mark_pano(city_id, has_pano):
    """Remember coverage so a barren city is never fetched twice."""
    with sqlite3.connect(DB) as db:
        db.execute("UPDATE cities SET pano=? WHERE id=?", (int(has_pano), city_id))


# ------------------------------------------------------------------------ geo


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def tile2deg(x, y, z, px, py, extent):
    """Tile-local point (y-up, 0..extent) -> lat/lon."""
    n = 2 ** z
    lon = (x + px / extent) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1 - py / extent) / n))))
    return lat, lon


@lru_cache(maxsize=512)
def place_name(lat, lon):
    """Human name for a point, e.g. "Shibuya, Japan".

    ponytail: Nominatim, no key needed. Its policy is 1 req/sec -- fine at the
    pace of a guessing game, and the cache absorbs repeats. Swap for a paid
    geocoder only if you put this in front of real traffic.
    """
    try:
        r = requests.get(
            NOMINATIM,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10,
                    "accept-language": "en"},
            headers={"User-Agent": UA},
            timeout=10,
        )
        addr = r.json().get("address", {})
    except (requests.RequestException, ValueError):
        return "unknown"
    local = next(
        (addr[k] for k in ("city", "town", "village", "municipality", "county", "state")
         if addr.get(k)),
        None,
    )
    country = addr.get("country")
    return ", ".join(p for p in (local, country) if p) or "the middle of nowhere"


# -------------------------------------------------------------------- imagery


def tile_panos(x, y):
    """360-only (image_id, lat, lon) from one z14 tile."""
    r = requests.get(
        f"{TILES}/{ZOOM}/{x}/{y}", params={"access_token": TOKEN}, timeout=60
    )
    r.raise_for_status()
    layer = mapbox_vector_tile.decode(r.content).get("image")
    if not layer:
        return ()
    extent = layer["extent"]
    feats = [f for f in layer["features"] if f["properties"].get("is_pano")]
    out = []
    for f in random.sample(feats, min(300, len(feats))):
        px, py = f["geometry"]["coordinates"]
        flat, flon = tile2deg(x, y, ZOOM, px, py, extent)
        out.append((f["properties"]["id"], flat, flon))
    return tuple(out)


@lru_cache(maxsize=32)
def city_images(lat, lon):
    """Sample of 360 images near a point, or () if the city has no coverage.

    ponytail: a z14 tile is only ~1 km wide, so a city centre often lands just
    outside its own coverage -- probing a few neighbouring tiles roughly doubles
    the hit rate. Stops at the first good one; empty tiles come back in ~1 s.
    """
    x, y = deg2tile(lat, lon, ZOOM)
    neighbours = [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                  if (dx, dy) != (0, 0)]
    random.shuffle(neighbours)
    for tx, ty in [(x, y)] + neighbours[:3]:
        found = tile_panos(tx, ty)
        if len(found) >= MIN_PANOS:
            return found
    return ()


def explore(city):
    """Fetch one city's tile and record what it held. Returns its images."""
    city_id, lat, lon = city
    try:
        images = city_images(lat, lon)
    except requests.RequestException:
        return ()
    mark_pano(city_id, bool(images))
    if images and (lat, lon) not in WARM:
        WARM.append((lat, lon))
    return images


def warm_one():
    """Cache one more city in the background, so rounds don't wait on a tile."""
    city = pick_city()
    if city and (city[1], city[2]) not in WARM:
        threading.Thread(target=explore, args=(city,), daemon=True).start()


def random_image():
    """Return (image_id, lat, lon) from a random city with 360 coverage.

    ponytail: serves from the warm set when possible -- a cold tile is a ~10 MB
    download and takes seconds. The background warmer keeps that set stocked and
    rotating, so the player sees new cities without ever waiting for one.
    """
    if WARM:
        images = city_images(*random.choice(WARM))
        if images:
            return random.choice(images)

    for _ in range(10):  # cold start, or the warm set went stale
        city = pick_city()
        if not city:
            break
        images = explore(city)
        if images:
            return random.choice(images)
    raise RuntimeError("no Mapillary 360 imagery found, try again")


# ------------------------------------------------------------------- routes


@app.route("/")
def index():
    return render_template("index.html", token=TOKEN)


MODES = {"time": (5, 10, 15), "rounds": (5, 10, 15, 30)}  # minutes / guesses


def time_left():
    """Seconds remaining in a Time Attack game, or None in Rounds mode.

    The clock only runs while a round is actually being played: `running_since`
    is None while a street is loading or a result is on screen, so waiting on a
    10 MB tile never costs the player time.
    """
    if session.get("mode") != "time":
        return None
    left = session.get("remaining", 0)
    if session.get("running_since"):
        left -= time.time() - session["running_since"]
    return max(0, round(left))


def pause_clock():
    if session.get("mode") == "time" and session.get("running_since"):
        session["remaining"] = time_left()
        session["running_since"] = None


def game_over():
    if session.get("mode") == "time":
        return time_left() == 0
    return session.get("played", 0) >= session.get("limit", 0)


@app.route("/api/start", methods=["POST"])
def start():
    body = request.get_json(silent=True) or {}
    mode, limit = body.get("mode"), body.get("limit")
    if mode not in MODES or limit not in MODES[mode]:
        return jsonify(error="pick a valid mode and length"), 400

    session.clear()
    session.update(score=0, played=0, mode=mode, limit=limit)
    if mode == "time":
        # starts paused; the clock runs once the client says a street is on screen
        session.update(remaining=limit * 60, running_since=None)
    return jsonify(score=0, mode=mode, limit=limit, seconds_left=time_left())


@app.route("/api/resume", methods=["POST"])
def resume():
    """Client calls this once the 360 view has finished loading."""
    if not session.get("mode"):
        return jsonify(error="no game in progress"), 400
    if game_over():
        return jsonify(seconds_left=0, over=True)
    # only a live round starts the clock -- never the result screen
    if (session.get("mode") == "time" and session.get("answer")
            and not session.get("running_since")):
        session["running_since"] = time.time()
    return jsonify(seconds_left=time_left(), over=False)


@app.route("/api/round")
def new_round():
    if not TOKEN:
        return jsonify(error="MAPILLARY_TOKEN env var not set"), 500
    if not session.get("mode"):
        return jsonify(error="no game in progress"), 400
    if game_over():
        return jsonify(error="game over"), 400
    pause_clock()  # loading a street is on the house
    try:
        image_id, lat, lon = random_image()
    except Exception as e:
        return jsonify(error=str(e)), 502
    warm_one()
    session["answer"] = (lat, lon)  # answer stays server-side, no devtools peeking
    return jsonify(image_id=str(image_id))


@app.route("/api/guess", methods=["POST"])
def guess():
    pause_clock()  # stop the clock the instant a guess lands
    answer = session.get("answer")
    if not answer:
        return jsonify(error="no active round"), 400
    g = request.get_json(silent=True) or {}
    try:
        lat, lon = float(g["lat"]), float(g["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="lat/lng required"), 400
    # the web map repeats sideways, so a click can arrive as lon=362 or -400.
    # haversine doesn't care, but the geocoder rejects anything outside +/-180.
    lat = max(-90.0, min(90.0, lat))
    lon = (lon + 180.0) % 360.0 - 180.0
    distance = haversine(lat, lon, answer[0], answer[1])
    points = round(MAX_SCORE * math.exp(-distance / FALLOFF_KM))
    total = session.get("score", 0) + points
    session["score"] = total
    session["played"] = session.get("played", 0) + 1
    session.pop("answer", None)  # one guess per round

    # round the guess for the cache key: 2dp is ~1 km, finer than a city name
    return jsonify(
        actual={"lat": answer[0], "lng": answer[1]},
        actual_name=place_name(round(answer[0], 2), round(answer[1], 2)),
        guess_name=place_name(round(lat, 2), round(lon, 2)),
        points=points,
        score=total,
        played=session["played"],
        limit=session.get("limit"),
        seconds_left=time_left(),
        over=game_over(),
    )


if not os.path.exists(DB):
    print("building cities.db from GeoNames, one time only...")
    print(f"  {build_db()} cities")

if __name__ == "__main__":
    for _ in range(4):
        warm_one()
    app.run(debug=True)
