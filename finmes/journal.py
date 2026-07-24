"""Generate hledger journal entries from parsed Txn records.

Deterministic write path: the journal is derived state, regenerable from raw
SMS + the mapping table. Dedup state lives in the journal itself as key: tags.
"""

import hashlib
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
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


def render_entry(txn: Txn, sender: str, mappings: Mappings) -> str:
    """One hledger entry. The amount sits on the account money flowed INTO:
    the category account for debits, the bank/card account for credits."""
    payee = txn.counterparty or synthesize_payee(txn)
    source = mappings.source_account(txn, sender)
    target = mappings.target_account(payee, txn.direction)
    tags = f"key:{dedup_key(txn)}"
    if txn.ref:
        tags += f", ref:{txn.ref}"
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
    records: Iterable[tuple[str, str, date]],
    journal_dir: Path,
    mappings: Mappings,
) -> Summary:
    """Parse (sender, body, received) records and append new entries to
    <year>.journal files. Idempotent: entries whose dedup key is already in
    the journal (or earlier in the same batch) are skipped."""
    journal_dir = Path(journal_dir)
    seen = existing_keys(journal_dir)
    summary = Summary()
    for sender, body, received in records:
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
            f.write(render_entry(txn, sender, mappings))
        summary.added += 1
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
