"""Smallest checks that fail if the geo maths or the city DB break."""
import sqlite3

from app import DB, MIN_POP, deg2tile, haversine, pick_city, tile2deg

# London -> Paris ~344 km; antipodal ~half circumference; same point = 0
assert abs(haversine(51.5, -0.13, 48.86, 2.35) - 344) < 5
assert abs(haversine(0, 0, 0, 180) - 20015) < 5
assert haversine(10, 20, 10, 20) == 0

# tile centre round-trips back to roughly the point it came from (~2 m at z14)
for lat, lon in [(52.516, 13.388), (-33.925, 18.424), (35.69, 139.70)]:
    x, y = deg2tile(lat, lon, 14)
    back = tile2deg(x, y, 14, 2048, 2048, 4096)
    assert haversine(lat, lon, *back) < 1.5, (lat, lon, back)

# the DB has cities and pick_city only returns eligible ones
with sqlite3.connect(DB) as db:
    assert db.execute("SELECT COUNT(*) FROM cities").fetchone()[0] > 30000
for _ in range(20):
    cid, lat, lon, country = pick_city()
    with sqlite3.connect(DB) as db:
        pop, pano = db.execute(
            "SELECT population, pano FROM cities WHERE id=?", (cid,)
        ).fetchone()
    assert pop >= MIN_POP and pano != 0
    assert -90 <= lat <= 90 and -180 <= lon <= 180
    assert country

# consecutive rounds must never repeat a country
for _ in range(15):
    banned = pick_city()[3]
    assert pick_city([banned])[3] != banned

print("ok")
