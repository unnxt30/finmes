"""Tests for deterministic hledger query helpers."""

from finmes import discord_queries as queries
from finmes.discord_queries import chunk_text


def test_spending_report_uses_expenses_and_period(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(queries, "run_hledger", lambda args, **_: captured.append(args) or "report")
    assert queries.spending_report("thismonth", None, ledger_file=tmp_path / "main.journal") == "report"
    assert captured == [["bal", "expenses", "-p", "thismonth", "-2", "-S"]]


def test_chunk_text_preserves_all_lines_under_discord_limit():
    chunks = chunk_text("a" * 1000 + "\n" + "b" * 1000, limit=1900)
    assert chunks == ["a" * 1000, "b" * 1000]
