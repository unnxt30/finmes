#!/usr/bin/env python3
"""nearby <lat> <lon> [radius_m] — POIs around a point, for the finmes agent.

Reads journal lat:/lon: tags' values as arguments and prints up to 10 places
within the radius (Mappls allows 500-10000 m; default 500), nearest first,
with full addresses. Needs MAPPLS_API_KEY (Mappls static REST key, passed as
access_token). Exits non-zero with a message on stderr on any failure —
callers must report, never guess.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

NEARBY_URL = "https://search.mappls.com/api/places/nearby/json"
# Broad OR-set (";" = or): payee review cares about anywhere money changes
# hands, not one category.
KEYWORDS = "shop;store;restaurant;food;market;clinic;salon;services"


def search_places(lat: float, lon: float, radius: float, key: str) -> dict:
    query = urllib.parse.urlencode({
        "keywords": KEYWORDS,
        "refLocation": f"{lat},{lon}",
        "radius": int(radius),
        "sortBy": "dist:asc",
        "access_token": key,
    })
    with urllib.request.urlopen(f"{NEARBY_URL}?{query}", timeout=15) as resp:
        return json.load(resp)


def format_places(data: dict) -> list[str]:
    rows = sorted(
        (p.get("distance", 0), p.get("placeName", "?"), p.get("placeAddress", "?"))
        for p in data.get("suggestedLocations", [])
    )
    return [f"{name} — {addr} — {dist} m" for dist, name, addr in rows[:10]]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    key = os.environ.get("MAPPLS_API_KEY")
    if not key:
        print("MAPPLS_API_KEY is not set", file=sys.stderr)
        return 2
    try:
        lat, lon = float(args[0]), float(args[1])
        # Mappls rejects radius <500 with a bare 400.
        radius = min(max(float(args[2]) if len(args) == 3 else 500.0, 500.0), 10000.0)
    except ValueError:
        print("lat, lon, and radius must be numeric", file=sys.stderr)
        return 2
    try:
        lines = format_places(search_places(lat, lon, radius, key))
    except Exception as e:  # urllib errors, bad JSON — report, never guess
        print(f"places lookup failed: {e}", file=sys.stderr)
        return 1
    if not lines:
        print(f"no places found within {radius:.0f} m", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
