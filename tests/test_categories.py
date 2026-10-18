"""Commuting rule and historical journal recategorization."""

import csv
from datetime import datetime
from decimal import Decimal

from finmes.categories import apply_review, recategorize, review_unknowns, suggest_category
from finmes.journal import Mappings
from finmes.server import handle


def test_commuting_rule_only_uncategorized_small_inr_debits_in_windows():
    mappings = Mappings(payees={"CAFE": "expenses:food:coffee"})
    def account(payee, amount, hour, direction="debit", currency="INR"):
        return mappings.target_account(
            payee, direction, amount=Decimal(amount), currency=currency,
            received_at=datetime(2026, 10, 13, hour, 30),
        )
    assert account("FARE", "53", 9) == "expenses:transport:commuting"
    assert account("FARE", "99.99", 19) == "expenses:transport:commuting"
    assert account("FARE", "55", 12) == "expenses:unknown"
    assert account("FARE", "55", 20) == "expenses:unknown"
    assert account("FARE", "100", 9) == "expenses:unknown"
    assert account("FARE", "55", 9, currency="USD") == "expenses:unknown"
    assert account("FARE", "55", 9, direction="credit") == "income:unknown"
    assert account("CAFE", "55", 9) == "expenses:food:coffee"


def test_merchant_suggestions_avoid_similar_person_names():
    assert suggest_category("MOHAMMED ZUBER") == ""
    assert suggest_category("SWIGGY INSTAMAR") == "expenses:groceries"
    assert suggest_category("Swiggy Diners") == ""


def test_phone_ingest_stores_receipt_time_and_commuting_category(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    body = ("ICICI Bank Acct XX973 debited for Rs 53.00 on 13-Oct-26; "
            "UNKNOWN FARE credited. UPI:123456789012.")
    payload = {"sender": "ios", "text": body,
               "received_at": "2026-10-13T09:35:00+05:30"}
    result = handle(payload, journal, Mappings(), tmp_path / "ingest.jsonl")
    assert result["status"] == "parsed"
    entry = (journal / "2026.journal").read_text()
    assert "expenses:transport:commuting  ₹53.00" in entry
    assert "time:09:35" in entry


def test_review_apply_and_recategorize_preserve_journal_details(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    year = journal / "2026.journal"
    year.write_text(
        "2026-10-13 BUS  ; key:bus.na.debit, lat:28.6\n"
        "    expenses:unknown  ₹53.00\n"
        "    assets:bank:test\n\n"
        "2026-10-13 CAFE  ; key:cafe.na.debit\n"
        "    expenses:unknown  ₹150.00\n"
        "    assets:bank:test\n"
    )
    original = year.read_text()
    mappings_path = tmp_path / "mappings.toml"
    mappings_path.write_text("[payees]\n")
    times = {"bus.na.debit": datetime(2026, 10, 13, 9, 30)}
    assert recategorize(journal, Mappings.load(mappings_path), times) == (1, 1)
    assert year.read_text() == original  # Preview is read-only.
    assert recategorize(journal, Mappings.load(mappings_path), times, apply=True) == (1, 1)
    assert "expenses:transport:commuting  ₹53.00" in year.read_text()
    assert "lat:28.6, time:09:30" in year.read_text()

    review = tmp_path / "review.tsv"
    assert review_unknowns(journal, review) == 1
    rows = list(csv.DictReader(review.open(), delimiter="\t"))
    assert rows[0]["payee"] == "CAFE"
    rows[0]["category"] = "expenses:food:coffee"
    with review.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    assert apply_review(review, mappings_path) == 1
    assert recategorize(journal, Mappings.load(mappings_path), {}, apply=True) == (1, 0)
    assert "expenses:food:coffee  ₹150.00" in year.read_text()
    assert "assets:bank:test" in year.read_text()
    assert recategorize(journal, Mappings.load(mappings_path), {}, apply=True) == (0, 0)
