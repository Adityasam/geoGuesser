# GeoGuesser 360°

A GeoGuessr-style game built on open data. You're dropped into a 360° street
photo somewhere on Earth, you drop a pin on the world map, and you're scored on
how close you got.

No Google Street View, no paid APIs, no map keys — Mapillary for the imagery,
GeoNames for the places, Nominatim for the place names, Esri for the map tiles.

---

## How it works

The interesting problem is *finding* a random street photo. Mapillary's Graph
API can't answer "give me a random image" — and its bbox search returns
`500 Please reduce the amount of data you're asking for` for any bounding box,
even a 400 m one. So the app works backwards from a list of real places:

1. **Pick a city.** A population-weighted random draw from 34,124 GeoNames
   cities held in a local SQLite table. Bigger cities come up more often
   because they're likelier to have coverage.
2. **Probe for imagery.** Fetch that city's Mapillary vector tile at zoom 14 —
   the only zoom whose public tiles carry the `image` layer. A z14 tile is only
   ~1 km wide, so a city centre often lands just outside its own coverage; the
   app also probes up to 3 random neighbouring tiles and takes the first with
   at least 10 panoramas.
3. **Remember the answer.** The result is written back to the city's `pano`
   column, so a barren city is never fetched twice. **The game gets faster the
   more you play**, and that knowledge survives restarts.
4. **Serve it.** 300 panoramas are sampled from the tile and cached. A
   background thread keeps a rolling set of 24 warm cities stocked, so rounds
   after the first are instant rather than waiting on a ~10 MB tile download.

Only `is_pano` (spherical) images are used, so every round is a full 360° view.

The true coordinates never reach the browser — they live in the Flask session
until you guess, so they can't be read out of devtools.

### A round

```mermaid
sequenceDiagram
    participant B as Browser
    participant S as Flask
    B->>S: GET /api/round
    Note over S: clock pauses<br/>(loading is free)
    S-->>B: image_id only
    B->>B: MapillaryJS moveTo()
    B->>S: POST /api/resume
    Note over S: clock starts
    B->>S: POST /api/guess {lat,lng}
    Note over S: clock pauses<br/>(results are free)
    S-->>B: points, score, real location
```

---

## Setup

Requires Python 3.9+.

**1. Get a Mapillary token** — sign in at
[mapillary.com/dashboard/developers](https://www.mapillary.com/dashboard/developers),
register an app, and copy the client token (it starts with `MLY|`).

**2. Configure and install:**

```bash
echo 'MAPILLARY_TOKEN=MLY|your|token' > .env
pip install -r requirements.txt
```

**3. Run:**

```bash
python app.py
```

Open http://127.0.0.1:5000.

On first run the app downloads GeoNames `cities15000.zip` (~3 MB) and builds
`cities.db`. That happens once and takes a few seconds. Delete the file to
rebuild it from scratch.

---

## Playing

Pick a mode from the title screen:

| Mode | Options | Ends when |
|---|---|---|
| **Time Attack** | 5 / 10 / 15 minutes | The clock runs out |
| **Fixed Rounds** | 5 / 10 / 15 / 30 rounds | You've used every guess |

In Time Attack the clock only runs while a street is actually on screen. It's
paused while a tile loads and while you're reading a result, so a slow download
never costs you time. The server is the authority on it; the browser countdown
is a mirror.

Drag inside the photo to look around and walk the arrows to move down the
street. Click the map bottom-right (it grows on hover), then **Guess** — the
map expands to full screen and shows the real location, the distance line, and
your points.

### Scoring

`points = 2000 × exp(−distance_km / 1500)`

Exponential decay, distance only — no country or continent matching, so a 2 km
guess across the Bosphorus scores 1,997 even though it's a different continent.

| Off by | Points |
|---|---|
| 0 km | 2,000 |
| 50 km | 1,934 |
| 250 km | 1,693 |
| 1,000 km | 1,027 |
| 3,000 km | 271 |
| 10,000 km | 3 |

The `1500` is GeoGuessr's constant (`5000 · exp(−10d / 14916 km`, the diagonal
of a world map). A linear curve was tried first and abandoned: the mean
distance between two random points on Earth is ~10,008 km, so a blind click
scored half marks, and being 250 km off cost only 1.25% versus a perfect guess.

---

## Tuning

All in [`app.py`](app.py):

| Constant | Default | Effect |
|---|---|---|
| `FALLOFF_KM` | `1500` | Lower punishes near misses harder |
| `MAX_SCORE` | `2000` | Points for a perfect guess |
| `MIN_POP` | `50000` | Lower for obscurer cities, at a worse coverage hit rate |
| `MIN_PANOS` | `10` | Minimum panoramas for a tile to count as covered |
| `WARM` maxlen | `24` | How many cities stay instantly available |

---

## Layout

```
app.py              Flask app: city selection, tile probing, scoring, routes
templates/          index.html — the whole front end, no build step
static/earth.mp4    title screen background
cities.db           generated on first run, not in git
test_app.py         geo maths + city DB checks
```

## API

| Route | Purpose |
|---|---|
| `POST /api/start` | `{mode, limit}` — begins a game, resets the score |
| `GET /api/round` | Returns an `image_id`, stashes the answer in the session |
| `POST /api/resume` | Client says the street is on screen; starts the clock |
| `POST /api/guess` | `{lat, lng}` — scores it, returns the real location |

## Tests

```bash
python test_app.py
```

Covers haversine against known distances, the tile ↔ lat/lon round-trip, and
`pick_city` eligibility.

---

## Notes and limits

- **Nominatim** (reverse geocoding) allows 1 request/sec and requires a real
  `User-Agent`. Two lookups per guess, cached on 2-decimal coordinates. Fine
  for solo play; swap for a paid geocoder before putting this in front of real
  traffic.
- **Coverage** is roughly 70% of eligible cities after neighbour probing.
  Mapillary is crowd-sourced, so it's thin in China, much of India, and rural
  areas generally.
- **`static/earth.mp4` is 24 MB.** Re-encode before deploying anywhere real:
  `ffmpeg -i static/earth.mp4 -c:v libx264 -crf 30 -an -movflags +faststart out.mp4`
- **Single player, single session.** Score lives in the Flask session cookie;
  there's no database of games, no leaderboard, no accounts.
- **Dev server only.** `app.run(debug=True)` — put it behind a real WSGI server
  if it ever leaves localhost, and set `SECRET_KEY` so sessions survive
  restarts.

## Credits

Imagery [Mapillary](https://www.mapillary.com) · Places
[GeoNames](https://www.geonames.org) (CC BY 4.0) · Geocoding
[Nominatim](https://nominatim.org)/OpenStreetMap · Map tiles Esri ·
Viewer [MapillaryJS](https://mapillary.github.io/mapillary-js/) · Map
[Leaflet](https://leafletjs.com)
