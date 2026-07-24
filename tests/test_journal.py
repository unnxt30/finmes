"""Tests for the journal generator: Txn -> hledger entries."""

from datetime import date
from decimal import Decimal
from typing import Any

from finmes.cli import main as cli_main
from finmes.journal import Mappings, dedup_key, existing_keys, ingest, render_entry, synthesize_payee
from finmes.parser import Txn


def txn(**kw: Any) -> Txn:
    defaults: dict[str, Any] = dict(
        date=date(2026, 6, 29),
        amount=Decimal("165.00"),
        direction="debit",
        counterparty="ZEPTOMARKETPLAC",
        account="XX973",
        channel="upi",
        ref="203007126186",
        raw="ICICI Bank Acct XX973 debited for Rs 165.00 on 29-Jun-26; ZEPTOMARKETPLAC credited. UPI:203007126186.",
    )
    defaults.update(kw)
    return Txn(**defaults)


MAPPINGS_TOML = """\
[accounts]
XX973 = "assets:bank:icici"
5250 = "liabilities:sbi-card"

[senders]
SBICRD = "liabilities:sbi-card"

[markers]
"SBI Credit Card" = "liabilities:sbi-card"

[payees]
Blinkit = "expenses:groceries"
"BIL*INFT*0011" = "assets:transfers"
salary = "income:salary"
"""


class TestMappings:
    def test_loads_tables_from_toml(self, tmp_path):
        p = tmp_path / "mappings.toml"
        p.write_text(MAPPINGS_TOML)
        m = Mappings.load(p)
        assert m.payees["Blinkit"] == "expenses:groceries"

    def test_source_account_from_masked_id(self):
        m = Mappings.loads(MAPPINGS_TOML)
        assert m.source_account(txn(account="XX973"), sender="ICICIT-S(x)") == "assets:bank:icici"

    def test_source_account_falls_back_to_sender_prefix(self):
        # SBI Card payment/cashback SMS carry no account number.
        m = Mappings.loads(MAPPINGS_TOML)
        assert m.source_account(txn(account=None), sender="SBICRD-S(smsft_fi)") == "liabilities:sbi-card"

    def test_source_account_falls_back_to_raw_marker(self):
        # iOS-automation payloads can't extract the real sender (sender="ios"),
        # so account-less SMS resolve via raw-text markers instead.
        m = Mappings.loads(MAPPINGS_TOML)
        t = txn(account=None, raw="We have received payment of Rs.100 via UPI & the same has been credited to your SBI Credit Card.")
        assert m.source_account(t, sender="ios") == "liabilities:sbi-card"

    def test_unknown_source_account_defaults(self):
        m = Mappings.loads(MAPPINGS_TOML)
        assert m.source_account(txn(account="Y999"), sender="NOBANK-S") == "assets:bank:unknown"

    def test_target_account_mapped_payee(self):
        m = Mappings.loads(MAPPINGS_TOML)
        assert m.target_account("Blinkit", "debit") == "expenses:groceries"

    def test_target_account_unknown_debit_and_credit(self):
        m = Mappings.loads(MAPPINGS_TOML)
        assert m.target_account("NAVIN TEA HOUSE", "debit") == "expenses:unknown"
        assert m.target_account("Some Employer", "credit") == "income:unknown"


class TestRenderEntry:
    def test_debit_with_mapped_payee(self):
        m = Mappings.loads(MAPPINGS_TOML)
        t = txn(counterparty="Blinkit")
        assert render_entry(t, "ICICIT-S(x)", m) == (
            "2026-06-29 Blinkit  ; key:203007126186.XX973.debit, ref:203007126186\n"
            "    expenses:groceries  ₹165.00\n"
            "    assets:bank:icici\n"
        )

    def test_debit_unknown_payee_goes_to_expenses_unknown(self):
        m = Mappings.loads(MAPPINGS_TOML)
        entry = render_entry(txn(), "ICICIT-S(x)", m)
        assert "    expenses:unknown  ₹165.00\n" in entry

    def test_credit_posts_amount_to_bank_account(self):
        m = Mappings.loads(MAPPINGS_TOML)
        t = txn(direction="credit", counterparty="Unnat Sharma", amount=Decimal("100000.00"))
        assert render_entry(t, "ICICIT-S(x)", m) == (
            "2026-06-29 Unnat Sharma  ; key:203007126186.XX973.credit, ref:203007126186\n"
            "    assets:bank:icici  ₹100000.00\n"
            "    income:unknown\n"
        )

    def test_no_ref_omits_ref_tag(self):
        m = Mappings.loads(MAPPINGS_TOML)
        entry = render_entry(txn(ref=None), "ICICIT-S(x)", m)
        assert "ref:" not in entry
        assert "key:" in entry

    def test_foreign_currency_renders_commodity_on_right(self):
        m = Mappings.loads(MAPPINGS_TOML)
        t = txn(account="5250", currency="SGD", amount=Decimal("1.38"), counterparty="ORACLESINGAPORE", ref=None)
        entry = render_entry(t, "SBICRD-S(x)", m)
        assert "    expenses:unknown  1.38 SGD\n" in entry
        assert "    liabilities:sbi-card\n" in entry

    def test_transfer_leg_via_payee_mapping(self):
        m = Mappings.loads(MAPPINGS_TOML)
        t = txn(counterparty="BIL*INFT*0011", ref=None, channel="other")
        entry = render_entry(t, "ICICIT-S(x)", m)
        assert "    assets:transfers  ₹165.00\n" in entry


class TestSynthesizePayee:
    def test_sbi_cashback(self):
        t = txn(counterparty=None, ref=None, account=None, direction="credit", channel="card",
                raw="Rs. 2378 has been credited to your CASHBACK SBI Card account for the month of Mar'26")
        assert synthesize_payee(t) == "SBI Card cashback"

    def test_sbi_card_payment(self):
        t = txn(counterparty=None, ref=None, account=None, direction="credit", channel="card",
                raw="We have received payment of Rs.49,869.43 via UPI & the same has been credited to your SBI Credit Card.")
        assert synthesize_payee(t) == "SBI Card payment"

    def test_icici_card_payment(self):
        t = txn(counterparty=None, ref=None, account="XX3002", direction="credit", channel="card",
                raw="Payment of Rs 22,680.00 has been received on your ICICI Bank Credit Card XX3002 on 30-JAN-26.")
        assert synthesize_payee(t) == "ICICI credit card payment"

    def test_upi_reversal(self):
        t = txn(counterparty=None, direction="credit",
                raw="Account XXX973 has been credited with Rs 60.00 on 23-Apr-26 as reversal of transaction with UPI: 912712042175.")
        assert synthesize_payee(t) == "UPI reversal"

    def test_iob_debit_gets_distinct_mappable_payee(self):
        # IOB debit SMS name no counterparty; "unknown" would collide with
        # every other faceless SMS and be unmappable as an exact string.
        t = txn(counterparty=None, ref=None, account="SB-xxx5472", channel="other",
                raw="Rs.150000.00 Debited to SB-xxx5472 AcBal:151790.00 CLRBal: 151790.00 [Mr Unnat ] CBO-CHENNAI on 22-06-2026 13:05:41.IOB.")
        assert synthesize_payee(t) == "IOB debit"

    def test_render_uses_synthesized_payee(self):
        m = Mappings.loads(MAPPINGS_TOML)
        t = txn(counterparty=None, ref=None, account=None, direction="credit", channel="card",
                raw="We have received payment of Rs.100 via UPI & the same has been credited to your SBI Credit Card.")
        entry = render_entry(t, "SBICRD-S(smsft_fi)", m)
        assert entry.startswith("2026-06-29 SBI Card payment  ;")
        assert "    liabilities:sbi-card  ₹165.00\n" in entry


SMS_ZEPTO = ("ICICIT-S(x)", "ICICI Bank Acct XX973 debited for Rs 165.00 on 29-Jun-26; ZEPTOMARKETPLAC credited. UPI:203007126186.", date(2026, 6, 29))
# Real corpus case: one self-transfer, two SMS sharing ref 642322423846.
SMS_XFER_DEBIT = ("SBIUPI-S(x)", "Dear UPI user A/C X1583 debited by 7000.00 on date 26Feb26 trf to UNNAT SHARMA Refno 642322423846", date(2026, 2, 26))
SMS_XFER_CREDIT = ("ICICIT-S(x)", "Dear Customer, Acct XX973 is credited with Rs 7000.00 on 26-Feb-26 from Unnat Sharma. UPI:642322423846", date(2026, 2, 26))


class TestIngest:
    def test_existing_keys_scans_journal_dir(self, tmp_path):
        (tmp_path / "2026.journal").write_text(
            "2026-06-29 Blinkit  ; key:203007126186.XX973.debit, ref:203007126186\n"
            "    expenses:groceries  ₹165.00\n"
            "    assets:bank:icici\n"
        )
        assert existing_keys(tmp_path) == {"203007126186.XX973.debit"}

    def test_ingest_appends_to_year_file(self, tmp_path):
        m = Mappings.loads(MAPPINGS_TOML)
        summary = ingest([SMS_ZEPTO], tmp_path, m)
        assert summary.added == 1
        content = (tmp_path / "2026.journal").read_text()
        assert "ZEPTOMARKETPLAC" in content

    def test_ingest_is_idempotent(self, tmp_path):
        m = Mappings.loads(MAPPINGS_TOML)
        ingest([SMS_ZEPTO], tmp_path, m)
        before = (tmp_path / "2026.journal").read_text()
        summary = ingest([SMS_ZEPTO], tmp_path, m)
        assert summary.added == 0
        assert summary.duplicate == 1
        assert (tmp_path / "2026.journal").read_text() == before

    def test_true_duplicate_sms_in_one_batch_written_once(self, tmp_path):
        m = Mappings.loads(MAPPINGS_TOML)
        summary = ingest([SMS_ZEPTO, SMS_ZEPTO], tmp_path, m)
        assert summary.added == 1
        assert summary.duplicate == 1

    def test_transfer_legs_share_ref_but_both_kept(self, tmp_path):
        m = Mappings.loads(MAPPINGS_TOML)
        summary = ingest([SMS_XFER_DEBIT, SMS_XFER_CREDIT], tmp_path, m)
        assert summary.added == 2

    def test_unparsed_and_skipped_counted_not_written(self, tmp_path):
        m = Mappings.loads(MAPPINGS_TOML)
        summary = ingest([("ICICIT-S(x)", "OTP is 123456 for txn", date(2026, 1, 1)),
                          ("AMZNIN-S(x)", "Your order has shipped", date(2026, 1, 1))], tmp_path, m)
        assert summary.added == 0
        assert summary.unparsed == 2
        assert not list(tmp_path.glob("*.journal"))

    def test_unknown_payees_reported(self, tmp_path):
        m = Mappings.loads(MAPPINGS_TOML)
        summary = ingest([SMS_ZEPTO], tmp_path, m)
        assert summary.unknown_payees == {"ZEPTOMARKETPLAC"}


class TestCli:
    def test_backfill_reads_tsv_and_reports(self, tmp_path, capsys):
        corpus = tmp_path / "sms.txt"
        corpus.write_text("2026-06-29 10:00:00\tICICIT-S(x)\t" + SMS_ZEPTO[1] + "\n")
        mappings = tmp_path / "mappings.toml"
        mappings.write_text(MAPPINGS_TOML)
        jdir = tmp_path / "journal"
        jdir.mkdir()
        rc = cli_main(["backfill", str(corpus), "--journal-dir", str(jdir), "--mappings", str(mappings)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "added: 1" in out
        assert "ZEPTOMARKETPLAC" in out  # unknown payee listed for review
        assert "ZEPTOMARKETPLAC" in (jdir / "2026.journal").read_text()


class TestDedupKey:
    def test_ref_based_key_includes_account_and_direction(self):
        # Self-transfers share one ref across both legs (SBI debit + ICICI
        # credit), so ref alone must not collide.
        assert dedup_key(txn()) == "203007126186.XX973.debit"

    def test_no_ref_falls_back_to_hash_of_raw_and_date(self):
        t = txn(ref=None, raw="Rs 100 spent on card", account="5250")
        key = dedup_key(t)
        assert key.endswith(".5250.debit")
        assert key == dedup_key(t)  # deterministic
        assert key != dedup_key(txn(ref=None, raw="Rs 100 spent on card", account="5250", date=date(2026, 6, 30)))

    def test_missing_account_uses_placeholder(self):
        assert dedup_key(txn(account=None)).split(".")[1] == "na"
