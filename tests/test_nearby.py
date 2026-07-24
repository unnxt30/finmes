"""Tests for the nearby POI helper. API calls are injected — no live Google."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "nearby", Path(__file__).parent.parent / "scripts" / "nearby.py")
nearby = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nearby)

CANNED = {
    "places": [
        {"displayName": {"text": "Raju Kirana Store"},
         "primaryTypeDisplayName": {"text": "Grocery store"},
         "location": {"latitude": 28.65368, "longitude": 77.44990}},
        {"displayName": {"text": "Santushti Sweets"},
         "primaryTypeDisplayName": {"text": "Sweet shop"},
         "location": {"latitude": 28.65460, "longitude": 77.45080}},
    ]
}


def test_format_places_sorts_by_distance_and_formats_lines():
    lines = nearby.format_places(CANNED, 28.65359, 77.44984)
    assert lines[0].startswith("Raju Kirana Store — Grocery store — ")
    assert lines[0].endswith(" m")
    assert "Santushti Sweets" in lines[1]


def test_format_places_handles_missing_type():
    data = {"places": [{"displayName": {"text": "X"},
                        "location": {"latitude": 28.65359, "longitude": 77.44984}}]}
    lines = nearby.format_places(data, 28.65359, 77.44984)
    assert lines[0].startswith("X — ? — ")


def test_main_without_api_key_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    rc = nearby.main(["28.65", "77.45"])
    assert rc != 0
    assert "GOOGLE_MAPS_API_KEY" in capsys.readouterr().err
