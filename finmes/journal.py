"""Generate hledger journal entries from parsed Txn records.

Deterministic write path: the journal is derived state, regenerable from raw
SMS + the mapping table. Dedup state lives in the journal itself as key: tags.
"""

import hashlib
import json
import re
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from finmes.parser import Txn, parse


@dataclass
class Mappings:
    """Ask-once mapping tables from mappings.toml.

    accounts: masked bank id (as it appears in SMS) -> hledger account
    senders:  SMS sender-id prefix -> hledger account (for SMS with no account number)
    payees:   exact payee string -> hledger account; an assets:/liabilities:
              target makes the entry a transfer leg, no special casing.
    """

    accounts: dict[str, str] = field(default_factory=dict)
    senders: dict[str, str] = field(default_factory=dict)
    markers: dict[str, str] = field(default_factory=dict)
    payees: dict[str, str] = field(default_factory=dict)

    @classmethod
    def loads(cls, text: str) -> "Mappings":
        data = tomllib.loads(text)
        return cls(
            accounts=data.get("accounts", {}),
            senders=data.get("senders", {}),
            markers=data.get("markers", {}),
            payees=data.get("payees", {}),
        )

    @classmethod
    def load(cls, path: Path) -> "Mappings":
        return cls.loads(Path(path).read_text())

    def source_account(self, txn: Txn, sender: str) -> str:
        """The bank/card account the money moved through."""
        if txn.account and txn.account in self.accounts:
            return self.accounts[txn.account]
        for prefix, account in self.senders.items():
            if sender.startswith(prefix):
                return account
        # iOS-automation payloads carry no real sender; match raw text instead.
        for marker, account in self.markers.items():
            if marker in txn.raw:
                return account
        return "assets:bank:unknown"

    def target_account(self, payee: str, direction: str) -> str:
        """The category (or transfer) account for the other side of the entry."""
        if payee in self.payees:
            return self.payees[payee]
        return "expenses:unknown" if direction == "debit" else "income:unknown"


def upsert_mapping(path: Path, table: str, key: str, account: str) -> None:
    if table not in {"payees", "markers"}:
        raise ValueError(f"unsupported mapping table: {table}")
    if not key or not account:
        raise ValueError("key and account must be non-empty")

    quoted_key = json.dumps(key)
    quoted_account = json.dumps(account)
    new_line = f"{quoted_key} = {quoted_account}"

    text = path.read_text() if path.exists() else ""
    section_re = re.compile(rf"^\[{re.escape(table)}\]\s*$", re.MULTILINE)
    match = section_re.search(text)

    if not match:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n[{table}]\n{new_line}\n"
        path.write_text(text)
        return

    start = match.end()
    next_section = re.search(r"^\[", text[start:], re.MULTILINE)
    end = start + next_section.start() if next_section else len(text)
    section_text = text[start:end]

    key_re = re.compile(rf"^{re.escape(quoted_key)}\s*=", re.MULTILINE)
    key_match = key_re.search(section_text)
    if key_match:
        line_end = section_text.find("\n", key_match.start())
        if line_end == -1:
            line_end = len(section_text)
        section_text = (
            section_text[:key_match.start()]
            + new_line
            + section_text[line_end:]
        )
    else:
        if section_text and not section_text.endswith("\n"):
            section_text += "\n"
        section_text += new_line + "\n"

    path.write_text(text[:start] + section_text + text[end:])


def read_ingest_log(path: Path):
    """Yield (sender, body, received, location) from the raw JSONL ingest log.
    Every logged payload goes back through parse() — replay decides, not the
    logged status (the parser may have improved since the message arrived)."""
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        p = rec.get("payload", rec)
        if p.get("received_at"):
            received = datetime.fromisoformat(p["received_at"]).date()
        else:
            received = datetime.fromisoformat(rec["at"]).date()
        yield p["sender"], p["text"], received, p.get("location")


_YEAR_JOURNAL_RE = re.compile(r"^\d{4}\.journal$")


# Deterministic descriptions for SMS that name no counterparty (card payment
# confirmations, cashback, reversals). Checked in order against the raw text.
_SYNTH_PAYEES = [
    ("CASHBACK SBI Card", "SBI Card cashback"),
    ("credited to your SBI Credit Card", "SBI Card payment"),
    ("ICICI Bank Credit Card", "ICICI credit card payment"),
    ("as reversal", "UPI reversal"),
    # IOB debit SMS never name the counterparty; a distinct payee keeps them
    # mappable (vs an "unknown" bucket shared across SMS types).
    ("Debited to SB-", "IOB debit"),
]


def synthesize_payee(txn: Txn) -> str:
    for marker, payee in _SYNTH_PAYEES:
        if marker in txn.raw:
            return payee
    return "unknown"


def _fmt_amount(txn: Txn) -> str:
    amt = txn.amount.quantize(Decimal("0.01"))
    return f"₹{amt}" if txn.currency == "INR" else f"{amt} {txn.currency}"


def _location_tags(location) -> str:
    """', lat:X, lon:Y' rendered into the tag comment, or ''. The iOS shortcut
    sends lat/lon as strings; coerce and round to 5 decimals (~1 m)."""
    if not location:
        return ""
    try:
        lat = round(float(location["lat"]), 5)
        lon = round(float(location["lon"]), 5)
    except (KeyError, TypeError, ValueError):
        return ""
    return f", lat:{lat}, lon:{lon}"


def render_entry(txn: Txn, sender: str, mappings: Mappings, location=None) -> str:
    """One hledger entry. The amount sits on the account money flowed INTO:
    the category account for debits, the bank/card account for credits."""
    payee = txn.counterparty or synthesize_payee(txn)
    source = mappings.source_account(txn, sender)
    target = mappings.target_account(payee, txn.direction)
    tags = f"key:{dedup_key(txn)}"
    if txn.ref:
        tags += f", ref:{txn.ref}"
    tags += _location_tags(location)
    amount = _fmt_amount(txn)
    if txn.direction == "debit":
        first, second = f"{target}  {amount}", source
    else:
        first, second = f"{source}  {amount}", target
    return (
        f"{txn.date.isoformat()} {payee}  ; {tags}\n"
        f"    {first}\n"
        f"    {second}\n"
    )


_KEY_RE = re.compile(r"\bkey:([^\s,]+)")


def existing_keys(journal_dir: Path) -> set[str]:
    """Dedup state = key: tags already in the journal. No sidecar files."""
    keys: set[str] = set()
    for path in Path(journal_dir).glob("*.journal"):
        keys.update(_KEY_RE.findall(path.read_text()))
    return keys


@dataclass
class Summary:
    added: int = 0
    duplicate: int = 0
    unparsed: int = 0
    unknown_payees: set[str] = field(default_factory=set)


def ingest(
    records: Iterable[tuple[str, str, date, dict | None]],
    journal_dir: Path,
    mappings: Mappings,
) -> Summary:
    """Parse (sender, body, received, location) records and append new entries
    to <year>.journal files. Idempotent: entries whose dedup key is already in
    the journal (or earlier in the same batch) are skipped."""
    journal_dir = Path(journal_dir)
    seen = existing_keys(journal_dir)
    summary = Summary()
    for sender, body, received, location in records:
        txn = parse(sender, body, received=received)
        if txn is None:
            summary.unparsed += 1
            continue
        key = dedup_key(txn)
        if key in seen:
            summary.duplicate += 1
            continue
        seen.add(key)
        payee = txn.counterparty or synthesize_payee(txn)
        if payee not in mappings.payees:
            summary.unknown_payees.add(payee)
        year_file = journal_dir / f"{txn.date.year}.journal"
        with year_file.open("a") as f:
            if year_file.stat().st_size:
                f.write("\n")
            f.write(render_entry(txn, sender, mappings, location=location))
        summary.added += 1
    return summary


def rebuild_journal(log_path: Path, journal_dir: Path, mappings: Mappings) -> Summary:
    journal_dir = Path(journal_dir)
    with tempfile.TemporaryDirectory(dir=journal_dir.parent) as temp:
        rebuilt = Path(temp)
        summary = ingest(read_ingest_log(log_path), rebuilt, mappings)
        for path in journal_dir.glob("*.journal"):
            if _YEAR_JOURNAL_RE.match(path.name):
                path.unlink()
        for path in rebuilt.glob("*.journal"):
            if _YEAR_JOURNAL_RE.match(path.name):
                path.replace(journal_dir / path.name)
    return summary


def dedup_key(txn: Txn) -> str:
    """Stable identity for a parsed SMS: <ref-or-hash>.<masked-acct>.<direction>.

    Ref alone is not unique — self-transfers emit two SMS (debit + credit legs)
    sharing one UPI/IMPS ref. The masked account is the raw SMS value, not the
    mapped hledger account, so keys survive mapping changes.
    """
    if txn.ref:
        ident = txn.ref
    else:
        ident = hashlib.sha1(f"{txn.date.isoformat()}|{txn.raw}".encode()).hexdigest()[:12]
    return f"{ident}.{txn.account or 'na'}.{txn.direction}"
