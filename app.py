import concurrent.futures
import io
import json
import math
import os
import random
import secrets
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

from regions import CONTINENT_OF, CONTINENTS, COUNTRY_NAMES

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "cities.db")
GEONAMES = "https://download.geonames.org/export/dump/cities15000.zip"

TOKEN = os.environ.get("MAPILLARY_TOKEN", "")
TILES = "https://tiles.mapillary.com/maps/vtp/mly1_public/2"
ZOOM = 14  # the only zoom where Mapillary's public tiles carry the image layer

MIN_POP = 50_000  # below this, Mapillary coverage gets thin enough to waste fetches
MIN_PANOS = 10    # a tile with 1 lonely image makes for a repetitive city
TILE_TRIES = 3    # Mapillary 5xx's sporadically; retry before giving up
MAX_SCORE = 2_000  # points for a perfect guess
# Exponential falloff: every FALLOFF_KM of error cuts the score to ~37% of it.
# 1500 km is GeoGuessr's constant (5000 * exp(-10d / 14916 km map diagonal)).
# Lower it to punish near misses harder; raise it to be more forgiving.
FALLOFF_KM = 1500
WARM = deque(maxlen=24)  # cities whose tile is cached and known to hold 360s

app = Flask(__name__)
# a random key per boot would log everyone out on every restart -- and with
# debug=True that is every file save. Persist one instead.
KEY_FILE = os.path.join(HERE, ".flask_secret")
if not os.environ.get("SECRET_KEY") and not os.path.exists(KEY_FILE):
    with open(KEY_FILE, "w") as fh:
        fh.write(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or open(KEY_FILE).read().strip()


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


def pick_city(exclude=(), only=None):
    """A random (id, lat, lon, country), skipping cities known to have no 360s.

    ponytail: population-weighted via `uniform / population`, taking the
    minimum -- a one-expression weighted sample. Big cities come up more often
    because they're likelier to have coverage, so fewer wasted tile fetches.

    `only`, when given, is an iterable of ISO country codes the draw is confined
    to -- the difficulty picker's Easy (one country) and Medium (one continent)
    scopes. None (the default) means the whole world, unchanged.
    """
    sql = """SELECT id, lat, lon, country FROM cities
             WHERE population >= ? AND pano IS NOT 0"""
    args = [MIN_POP]
    exclude = [c for c in exclude if c]
    if exclude:
        sql += " AND country NOT IN (%s)" % ",".join("?" * len(exclude))
        args += exclude
    if only:
        only = list(only)
        sql += " AND country IN (%s)" % ",".join("?" * len(only))
        args += only
    sql += """ ORDER BY (ABS(RANDOM()) % 1000000) / CAST(population AS REAL)
              LIMIT 1"""
    with sqlite3.connect(DB) as db:
        return db.execute(sql, args).fetchone()


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


# -------------------------------------------------------------------- imagery


class TileUnavailable(Exception):
    """Mapillary was unreachable or erroring -- says nothing about coverage."""


def fetch_tile(x, y):
    """One tile's bytes, riding out Mapillary's intermittent 5xx.

    Their tile and graph endpoints both return sporadic
    `500 Service temporarily unavailable`. Treating that as "this city has no
    imagery" would be wrong twice over: it fails the round, and it would poison
    the `pano` column for a city that is actually fine.
    """
    for attempt in range(TILE_TRIES):
        try:
            r = requests.get(f"{TILES}/{ZOOM}/{x}/{y}",
                             params={"access_token": TOKEN}, timeout=30)
            if r.status_code < 500:
                r.raise_for_status()
                return r.content
        except requests.RequestException:
            pass
        if attempt < TILE_TRIES - 1:
            time.sleep(0.5 * (attempt + 1))
    raise TileUnavailable(f"tile {ZOOM}/{x}/{y} unavailable")


def tile_panos(x, y):
    """360-only (image_id, lat, lon) from one z14 tile."""
    layer = mapbox_vector_tile.decode(fetch_tile(x, y)).get("image")
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

    The home tile is tried first (usually a hit); on a miss the 3 neighbours are
    fetched concurrently, so a miss costs one tile's latency instead of four.
    """
    x, y = deg2tile(lat, lon, ZOOM)
    home = tile_panos(x, y)
    if len(home) >= MIN_PANOS:
        return home
    neighbours = [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                  if (dx, dy) != (0, 0)]
    random.shuffle(neighbours)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(tile_panos, tx, ty) for tx, ty in neighbours[:3]]
        for fut in concurrent.futures.as_completed(futures):
            try:
                found = fut.result()
            except requests.RequestException:
                continue
            if len(found) >= MIN_PANOS:
                return found
    return ()


def explore(city):
    """Fetch one city's tile and record what it held. Returns its images."""
    city_id, lat, lon, country = city
    try:
        images = city_images(lat, lon)
    except (requests.RequestException, TileUnavailable):
        return ()          # a failed probe proves nothing; leave `pano` alone
    mark_pano(city_id, bool(images))
    if images and (country, lat, lon) not in WARM:
        WARM.append((country, lat, lon))
    return images


def warm_one(scope=None):
    """Cache one more city in the background, so rounds don't wait on a tile.

    `scope`, when given, keeps the warm pool stocked with cities the current
    game can actually use. Without it an Easy/Medium game warms world cities it
    then filters out, so every round pays a cold tile fetch.
    """
    stocked = {w[0] for w in WARM}
    if scope:
        city = pick_city(stocked, only=scope) or pick_city(only=scope)
    else:
        city = pick_city(stocked) or pick_city()   # prefer a country we lack
    if city and (city[3], city[1], city[2]) not in WARM:
        threading.Thread(target=explore, args=(city,), daemon=True).start()


RECENT_COUNTRIES = 4   # how far back to avoid repeating a country


def random_image(recent=(), scope=None):
    """Return (image_id, lat, lon, country) from a random covered city.

    `recent` is the countries just played, most recent first. The newest one is
    a hard no -- consecutive rounds are never in the same country. The rest are
    a preference, so a thin warm set degrades to variety-when-possible instead
    of ping-ponging between two countries.

    `scope`, when given, is a set of ISO country codes every round must come
    from -- the Easy (one country) and Medium (one continent) difficulties.
    A one-country scope drops the no-repeat rule, since by definition every
    round is that same country.
    """
    recent = [c for c in recent if c]
    if scope is not None and len(scope) < 2:
        recent = []
    previous, older = (recent[0], set(recent[1:])) if recent else (None, set())

    def in_scope(country):
        return scope is None or country in scope

    pool = [w for w in WARM if w[0] != previous and in_scope(w[0])]
    fresh = [w for w in pool if w[0] not in older]

    def from_warm(candidates):
        for country, lat, lon in random.sample(candidates, len(candidates)):
            try:
                images = city_images(lat, lon)
            except (requests.RequestException, TileUnavailable):
                continue   # this one is unreachable right now; try another
            if images:
                return random.choice(images) + (country,)
        return None

    def cold_hit(city):
        images = explore(city)
        return random.choice(images) + (city[3],) if images else None

    def from_cold(avoid):
        # probe several candidate cities at once and take the first with
        # coverage -- a thin scope (much of Asia, Africa) can otherwise walk
        # through a dozen barren cities one slow tile at a time
        budget = 12 if scope else 6
        while budget > 0:
            batch = [c for c in (pick_city(avoid, scope)
                                 for _ in range(min(4, budget))) if c]
            if not batch:
                return None
            budget -= len(batch)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(cold_hit, c) for c in batch]
                for fut in concurrent.futures.as_completed(futures):
                    found = fut.result()
                    if found:
                        return found
        return None

    # a cold fetch costs a few seconds but adds a new country to the warm pool,
    # which beats ping-ponging between the two or three already in it
    for attempt in (lambda: from_warm(fresh),
                    lambda: from_cold(recent),
                    lambda: from_warm(pool),
                    lambda: from_cold([previous])):
        found = attempt()
        if found:
            return found
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


def begin(mode, limit, code=None):
    """Reset the session for a new game. Shared by solo, create and join."""
    session.clear()
    session.update(score=0, played=0, mode=mode, limit=limit)
    if code:
        session["game"] = code
        session["pid"] = secrets.token_hex(8)
    if mode == "time":
        # starts paused; the clock runs once the client says a street is on screen
        session.update(remaining=limit * 60, running_since=None)
    return jsonify(score=0, mode=mode, limit=limit, code=code,
                   seconds_left=time_left())



# ---------------------------------------------------------------- difficulty
#
# A difficulty pick only narrows which countries a round is drawn from. Scoring,
# the timer, round counts and the result screen are all untouched.


def difficulty_scope(difficulty, area):
    """Resolve a difficulty pick to the ISO country codes it permits.

    None  -- Hard, or nothing sent: the whole world, no filter (unchanged).
    set   -- Easy (the one chosen country) or Medium (a continent's countries).
    False -- the pick was malformed; the caller should answer 400.
    """
    if not difficulty or difficulty == "hard":
        return None
    if difficulty == "easy":
        code = str(area or "").strip().upper()
        return {code} if code in CONTINENT_OF else False
    if difficulty == "medium":
        want = str(area or "").strip().lower()
        picked = {c for c, cont in CONTINENT_OF.items() if cont.lower() == want}
        return picked or False
    return False


@app.route("/api/regions")
def regions():
    """Countries and continents the difficulty picker may offer -- limited to
    the ones the local cities table actually holds."""
    with sqlite3.connect(DB) as db:
        have = {r[0] for r in db.execute(
            "SELECT DISTINCT country FROM cities WHERE population >= ?", (MIN_POP,)
        )}
    countries = sorted(
        ({"code": c, "name": COUNTRY_NAMES.get(c, c), "continent": CONTINENT_OF[c]}
         for c in have if c in CONTINENT_OF),
        key=lambda d: d["name"],
    )
    return jsonify(continents=CONTINENTS, countries=countries)


@app.route("/api/start", methods=["POST"])
def start():
    body = request.get_json(silent=True) or {}
    mode, limit = body.get("mode"), body.get("limit")
    if mode not in MODES or limit not in MODES[mode]:
        return jsonify(error="pick a valid mode and length"), 400
    scope = difficulty_scope(body.get("difficulty"), body.get("area"))
    if scope is False:
        return jsonify(error="pick a valid difficulty option"), 400
    resp = begin(mode, limit)
    if scope:
        session["scope"] = sorted(scope)  # ISO codes every round must match
        for _ in range(5):                 # stock the pool for this scope up front
            warm_one(scope)
    return resp


@app.route("/api/resume", methods=["POST"])
def resume():
    """The client says a street is on screen -- start the Time Attack clock.

    Each round loads paused (pause_clock in /api/round and /api/guess), so this
    is what actually gets the clock ticking again. A no-op outside Time Attack.
    """
    if session.get("mode") == "time" and not game_over() \
            and not session.get("running_since"):
        session["running_since"] = time.time()
    return jsonify(seconds_left=time_left(), over=game_over())


# ------------------------------------------------------------- multiplayer
#
# Games are kept in memory for speed and mirrored into SQLite after every
# change, because `debug=True` restarts the server on each file save -- which
# used to drop every game in progress and fail joins with "no game with that
# code". The dict is the working copy; the table is what survives a restart.

GAMES = {}
GAMES_LOCK = threading.Lock()
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to mistype
CODE_LEN = 10
GAME_TTL = 12 * 3600


def games_table():
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS games("
                   "code TEXT PRIMARY KEY, data TEXT, created REAL)")


def save_game(code):
    """Mirror a game to disk. Call inside GAMES_LOCK, after every change."""
    game = GAMES.get(code)
    if not game:
        return
    blob = dict(game, ready=sorted(game["ready"]))   # a set is not JSON
    with sqlite3.connect(DB) as db:
        db.execute("INSERT OR REPLACE INTO games(code, data, created) VALUES(?,?,?)",
                   (code, json.dumps(blob), game["created"]))


def get_game(code):
    """The live game for a code, reloading it from disk after a restart."""
    if not code:
        return None
    if code in GAMES:
        return GAMES[code]
    with sqlite3.connect(DB) as db:
        row = db.execute("SELECT data FROM games WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    game = json.loads(row[0])
    game["ready"] = set(game["ready"])
    # json makes them lists; pad rounds saved before countries were tracked
    game["rounds"] = [tuple(r) if len(r) == 4 else tuple(r) + (None,)
                      for r in game["rounds"]]
    GAMES[code] = game
    return game


def new_code():
    while True:
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN))
        if get_game(code) is None:
            return code


def shared_round(code, index):
    """Location for round `index` of a game: generated once, same for everyone."""
    game = get_game(code)
    while True:
        with GAMES_LOCK:
            if index < len(game["rounds"]):
                return game["rounds"][index]
            recent = [r[3] for r in reversed(game["rounds"][-RECENT_COUNTRIES:])]
        found = random_image(recent)  # slow, so never hold the lock across it
        with GAMES_LOCK:
            if index < len(game["rounds"]):
                return game["rounds"][index]   # another player got there first
            game["rounds"].append(found)
            save_game(code)


MP_ROUNDS = 10       # multiplayer is always a 10-round match, no options
STALE = 90           # a player silent this long stops holding the round up


def touch(game):
    """Mark this player alive, so a closed tab can't freeze everyone else."""
    p = game["players"].get(session.get("pid"))
    if p:
        p["seen"] = time.time()


def active_players(game):
    cutoff = time.time() - STALE
    return {pid: p for pid, p in game["players"].items() if p["seen"] > cutoff}


def sync_state(game):
    """Who still owes a guess, and who still owes a click of Next."""
    active = active_players(game)
    all_guessed = all(pid in game["guessed"] for pid in active)
    all_ready = all(pid in game["ready"] for pid in active)
    # the waiting lists name *other* people; you already know about yourself
    me_pid = session.get("pid")
    waiting_guess = [p["name"] for pid, p in active.items()
                     if pid not in game["guessed"] and pid != me_pid]
    waiting_ready = [p["name"] for pid, p in active.items()
                     if pid not in game["ready"] and pid != me_pid]
    # other players' pins are only revealed to someone who has already guessed
    me = session.get("pid")
    others = [
        {"name": game["players"][pid]["name"], "points": g["points"],
         "lat": g["lat"], "lng": g["lng"], "id": pid}
        for pid, g in game["guessed"].items()
        if pid != me and pid in game["players"] and g.get("lat") is not None
    ] if me in game["guessed"] else []

    return {
        "you": me,
        "guesses": others,
        "round": game["round"],
        "limit": game["limit"],
        "all_guessed": all_guessed,
        "all_ready": all_ready,
        "waiting_on": waiting_guess or waiting_ready,
        "you_guessed": session.get("pid") in game["guessed"],
        "you_ready": session.get("pid") in game["ready"],
        "players": sorted(
            [{"id": pid, "name": p["name"], "score": p["score"],
              "played": p["played"], "guessed": pid in game["guessed"]}
             for pid, p in game["players"].items()],
            key=lambda p: -p["score"],
        ),
    }


def clean_name(name, fallback):
    """Trim a player-supplied name to something safe to show on a scoreboard."""
    name = " ".join(str(name or "").split())[:20]
    return name or fallback


def join_game(code, name=None):
    game = get_game(code)
    if not game:
        return jsonify(error="no game with that code"), 404
    body = begin(game["mode"], game["limit"], code)
    with GAMES_LOCK:
        pid = session["pid"]
        game["players"][pid] = {
            "name": clean_name(name, "Player %d" % (len(game["players"]) + 1)),
            "score": 0,
            "played": 0,
            "seen": time.time(),
        }
        # joining mid-round: sit this one out rather than stalling everyone
        if game["guessed"]:
            game["guessed"][pid] = {"points": 0, "lat": None, "lng": None}
            game["ready"].add(pid)
        save_game(code)
    return body


@app.route("/api/create", methods=["POST"])
def create():
    cutoff = time.time() - GAME_TTL
    with GAMES_LOCK:
        for stale in [c for c, g in GAMES.items() if g["created"] < cutoff]:
            del GAMES[stale]
        with sqlite3.connect(DB) as db:
            db.execute("DELETE FROM games WHERE created < ?", (cutoff,))
        code = new_code()
        GAMES[code] = {"mode": "rounds", "limit": MP_ROUNDS, "rounds": [],
                       "players": {}, "created": time.time(),
                       "round": 0, "guessed": {}, "ready": set()}
        save_game(code)
    return join_game(code, (request.get_json(silent=True) or {}).get("name"))


@app.route("/api/join", methods=["POST"])
def join():
    body = request.get_json(silent=True) or {}
    return join_game(str(body.get("code", "")).strip().upper(), body.get("name"))


@app.route("/api/state")
def state():
    """Is this browser still in a game? Used to survive a page reload."""
    game = get_game(session.get("game"))
    if not game or session.get("pid") not in game["players"]:
        return jsonify(active=False)
    if game_over():
        return jsonify(active=False)          # finished: send them to the title
    with GAMES_LOCK:
        touch(game)
        return jsonify(active=True, code=session["game"], mode=game["mode"],
                       score=session.get("score", 0),
                       seconds_left=time_left(), **sync_state(game))


@app.route("/api/quit", methods=["POST"])
def quit_game():
    """Leave a game. In multiplayer, stop holding the other players up."""
    game = get_game(session.get("game"))
    if game:
        with GAMES_LOCK:
            pid = session.get("pid")
            game["players"].pop(pid, None)
            game["guessed"].pop(pid, None)
            game["ready"].discard(pid)
            # the quitter may have been the last one everyone was waiting on
            if game["players"] and not set(active_players(game)) - game["ready"]:
                game["round"] += 1
                game["guessed"] = {}
                game["ready"] = set()
            save_game(session["game"])
    session.clear()
    return jsonify(ok=True)


@app.route("/api/sync")
def sync():
    game = get_game(session.get("game"))
    if not game:
        return jsonify(error="not in a multiplayer game"), 400
    with GAMES_LOCK:
        touch(game)
        save_game(session["game"])
        return jsonify(code=session["game"], **sync_state(game))


@app.route("/api/ready", methods=["POST"])
def ready():
    """This player has seen the result and wants the next street."""
    game = get_game(session.get("game"))
    if not game:
        return jsonify(error="not in a multiplayer game"), 400
    with GAMES_LOCK:
        touch(game)
        if session["pid"] not in game["guessed"]:
            return jsonify(error="guess first", **sync_state(game)), 400
        game["ready"].add(session["pid"])
        if not set(active_players(game)) - game["ready"]:
            game["round"] += 1          # everyone's in: move the match along
            game["guessed"] = {}
            game["ready"] = set()
        save_game(session["game"])
        return jsonify(**sync_state(game))


@app.route("/api/round")
def new_round():
    if not TOKEN:
        return jsonify(error="MAPILLARY_TOKEN env var not set"), 500
    if not session.get("mode"):
        return jsonify(error="no game in progress"), 400
    if game_over():
        return jsonify(error="game over"), 400
    pause_clock()  # loading a street is on the house

    # a round is fixed until it is answered: asking again must not reroll it
    if session.get("answer") and session.get("image"):
        return jsonify(image_id=session["image"])
    try:
        mp = get_game(session.get("game"))
        if mp:
            image_id, lat, lon, country = shared_round(session["game"], mp["round"])
        else:
            scope = session.get("scope")
            image_id, lat, lon, country = random_image(
                session.get("recent", []), set(scope) if scope else None
            )
    except RuntimeError as e:
        # nothing found right now (often Mapillary being flaky) -- retryable
        return jsonify(error=str(e), retry=True), 503
    except Exception as e:
        return jsonify(error=str(e)), 502
    warm_scope = set(session["scope"]) if session.get("scope") else None
    # keep a few tiles ahead so later rounds don't wait; a narrowed scope needs
    # a deeper buffer because it has fewer fresh countries to fall back on
    for _ in range(3 if warm_scope else 2):
        warm_one(warm_scope)
    session["answer"] = (lat, lon)  # answer stays server-side, no devtools peeking
    session["image"] = str(image_id)
    session["recent"] = ([country] + session.get("recent", []))[:RECENT_COUNTRIES]
    return jsonify(image_id=session["image"])


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
    session.pop("image", None)

    game = get_game(session.get("game"))
    if game and session["pid"] in game["players"]:
        with GAMES_LOCK:
            touch(game)
            game["players"][session["pid"]].update(
                score=total, played=session["played"]
            )
            game["guessed"][session["pid"]] = {"points": points,
                                               "lat": lat, "lng": lon}
            save_game(session["game"])

    return jsonify(
        actual={"lat": answer[0], "lng": answer[1]},
        distance_km=round(distance, 1),
        points=points,
        score=total,
        played=session["played"],
        limit=session.get("limit"),
        seconds_left=time_left(),
        over=game_over(),
        sync=sync_state(game) if game else None,
    )


# order matters: games_table() creates cities.db, which would make the check
# below think the city table already exists
if not os.path.exists(DB):
    print("building cities.db from GeoNames, one time only...")
    print(f"  {build_db()} cities")

games_table()

if __name__ == "__main__":
    for _ in range(6):   # a spread of countries ready before the first round
        warm_one()
    app.run(debug=True)
