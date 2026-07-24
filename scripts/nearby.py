#!/usr/bin/env python3
"""nearby <lat> <lon> [radius_m] — POIs around a point, for the finmes agent.

Reads journal lat:/lon: tags' values as arguments and prints up to 10 places
within the radius (default 150 m), nearest first, plus the locality. Needs
GOOGLE_MAPS_API_KEY (Places API New + Geocoding API). Exits non-zero with a
message on stderr on any failure — callers must report, never guess.
"""

import json
import math
import os
import sys
import urllib.request

PLACES_URL = "https://places.googleapis.com/v1/places:searchNearby"
GEOCODE_URL = ("https://maps.googleapis.com/maps/api/geocode/json"
               "?latlng={lat},{lon}&result_type=locality|sublocality&key={key}")


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


def search_places(lat: float, lon: float, radius: float, key: str) -> dict:
    body = json.dumps({
        "maxResultCount": 10,
        "rankPreference": "DISTANCE",
        "locationRestriction": {"circle": {
            "center": {"latitude": lat, "longitude": lon}, "radius": radius}},
    }).encode()
    req = urllib.request.Request(PLACES_URL, data=body, headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask":
            "places.displayName,places.primaryTypeDisplayName,places.location",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def locality(lat: float, lon: float, key: str) -> str | None:
    url = GEOCODE_URL.format(lat=lat, lon=lon, key=key)
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    results = data.get("results")
    return results[0]["formatted_address"] if results else None


def format_places(data: dict, lat: float, lon: float) -> list[str]:
    rows = []
    for p in data.get("places", []):
        loc = p["location"]
        rows.append((
            _distance_m(lat, lon, loc["latitude"], loc["longitude"]),
            p["displayName"]["text"],
            p.get("primaryTypeDisplayName", {}).get("text", "?"),
        ))
    rows.sort()
    return [f"{name} — {kind} — {dist} m" for dist, name, kind in rows]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        print("GOOGLE_MAPS_API_KEY is not set", file=sys.stderr)
        return 2
    lat, lon = float(args[0]), float(args[1])
    radius = min(float(args[2]) if len(args) == 3 else 150.0, 1000.0)
    try:
        lines = format_places(search_places(lat, lon, radius, key), lat, lon)
    except Exception as e:  # urllib errors, bad JSON — report, never guess
        print(f"places lookup failed: {e}", file=sys.stderr)
        return 1
    if not lines:
        print(f"no places found within {radius:.0f} m", file=sys.stderr)
        return 1
    try:
        area = locality(lat, lon, key)
    except Exception:
        area = None
    if area:
        print(f"near: {area}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
