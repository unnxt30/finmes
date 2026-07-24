"""Tests for the nearby POI helper. API calls are injected — no live Mappls."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "nearby", Path(__file__).parent.parent / "scripts" / "nearby.py")
nearby = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nearby)

CANNED = {
    "suggestedLocations": [
        {"distance": 167, "placeName": "Homemade Cravings",
         "placeAddress": "3F-200, Nehru Nagar, Ghaziabad, Uttar Pradesh, 201001",
         "keywords": ["FODOTH"], "type": "POI"},
        {"distance": 82, "placeName": "Eatery Stall",
         "placeAddress": "Block F, Nehru Nagar 3, Ghaziabad, Uttar Pradesh, 201001",
         "keywords": ["FODOTH"], "type": "POI"},
    ]
}


def test_format_places_sorts_by_distance_and_formats_lines():
    lines = nearby.format_places(CANNED)
    assert lines[0] == "Eatery Stall — Block F, Nehru Nagar 3, Ghaziabad, Uttar Pradesh, 201001 — 82 m"
    assert lines[1].startswith("Homemade Cravings — ")
    assert lines[1].endswith(" — 167 m")


def test_format_places_handles_missing_address():
    data = {"suggestedLocations": [{"distance": 5, "placeName": "X"}]}
    lines = nearby.format_places(data)
    assert lines[0] == "X — ? — 5 m"


def test_main_with_non_numeric_args_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setenv("MAPPLS_API_KEY", "dummy")
    rc = nearby.main(["abc", "77.45"])
    assert rc == 2
    assert "numeric" in capsys.readouterr().err


def test_main_without_api_key_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("MAPPLS_API_KEY", raising=False)
    rc = nearby.main(["28.65", "77.45"])
    assert rc != 0
    assert "MAPPLS_API_KEY" in capsys.readouterr().err
