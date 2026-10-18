import json

import pytest

from finmes import enrichment as api


def test_decisions_request_minimizes_data_and_validates_category(monkeypatch):
    captured = []
    def fake(req):
        captured.append(req)
        return {"answers": [{"type": "choice", "name": "category", "choice": "expenses:groceries", "confidence": .8}]}
    monkeypatch.setattr(api, "_json_request", fake)
    result = api.decide_category({"payee": "Shop", "examples": [{"date": "2026-10-18", "amount": "₹500.00",
        "direction": "debit", "raw": "SECRET SMS", "key": "UPI-SECRET", "location": {"lat": 28.6, "lon": 77.2}}]}, [], "test-key")
    assert result.category == "expenses:groceries"
    body = json.loads(captured[0].data)
    assert body["model"] == "gpt-6-luna"
    assert captured[0].full_url == "https://api.openai.com/v1/decisions"
    assert "SECRET" not in body["input"] and '"location"' not in body["input"]
    assert body["questions"][0]["type"] == "choice"


@pytest.mark.parametrize("answer", [
    {"type": "choice", "name": "category", "choice": "invented", "confidence": .9},
    {"type": "choice", "name": "category", "choice": "expenses:groceries", "confidence": float("nan")},
    {"type": "choice", "name": "category", "choice": "expenses:groceries", "confidence": True},
    {"type": "choice", "name": "category", "choice": "expenses:groceries", "confidence": 2},
    {"type": "predicate", "name": "category", "probability": .9},
])
def test_bad_decisions_answers_are_not_categories(monkeypatch, answer):
    monkeypatch.setattr(api, "_json_request", lambda req: {"answers": [answer]})
    with pytest.raises(api.EnrichmentError):
        api.decide_category({"payee": "Shop", "examples": []}, [], "key")


@pytest.mark.parametrize("location", [{"lat": "nan", "lon": 77}, {"lat": 91, "lon": 77}, {"lat": 28, "lon": -181}, None])
def test_invalid_gps_cannot_trigger_lookup(location):
    assert api.coordinates(location) is None


def test_refusal_and_unknown_do_not_suggest_a_category(monkeypatch):
    candidate = {"payee": "Someone", "examples": []}
    for answer in ({"type": "refusal", "name": "category"},
                   {"type": "choice", "name": "category", "choice": "unknown", "confidence": .9}):
        monkeypatch.setattr(api, "_json_request", lambda req: {"answers": [answer]})
        assert api.decide_category(candidate, [], "key").category is None
