"""Review and safely update categories already stored in hledger journals."""

import csv
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from finmes.journal import Mappings, dedup_key, journal_lock
from finmes.messages import bank_sms
from finmes.parser import parse

HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (.*?)  ; (.*)$")
KEY_RE = re.compile(r"\bkey:([^\s,]+)")
TIME_RE = re.compile(r"\btime:(\d{2}:\d{2})\b")
UNKNOWN_RE = re.compile(r"^(\s+)(expenses:unknown|income:unknown)(\s{2}.*)?$")
INR_RE = re.compile(r"^₹([\d,]+(?:\.\d+)?)$")
ACCOUNT_RE = re.compile(r"^(?:expenses|income|assets|liabilities):[a-zA-Z0-9:_-]+$")
SUGGESTIONS = [
    (re.compile(r"blinkit|zepto|bigbasket|instamar", re.I), "expenses:groceries"),
    (re.compile(r"zomato|swiggy", re.I), "expenses:food:delivery"),
    (re.compile(r"air\s?india|ixigo|indigo", re.I), "expenses:travel"),
    (re.compile(r"\b(?:uber|rapido|metro)", re.I), "expenses:transport:commuting"),
]


def suggest_category(payee: str) -> str:
    if re.search(r"swiggy diners", payee, re.I):
        return ""
    return next((account for pattern, account in SUGGESTIONS if pattern.search(payee)), "")


def _entries(journal_dir: Path):
    for path in sorted(Path(journal_dir).glob("[0-9][0-9][0-9][0-9].journal")):
        for block in path.read_text().split("\n\n"):
            lines = block.splitlines()
            if len(lines) == 3 and (match := HEADER_RE.match(lines[0])):
                yield path, lines, match


def _unknown(lines: list[str]):
    for index in (1, 2):
        if match := UNKNOWN_RE.match(lines[index]):
            direction = "debit" if match.group(2).startswith("expenses:") else "credit"
            amount = None
            if direction == "debit" and match.group(3):
                if amt := INR_RE.match(match.group(3).strip()):
                    amount = Decimal(amt.group(1).replace(",", ""))
            return index, match, direction, amount
    return None


def review_unknowns(journal_dir: Path, output: Path) -> int:
    """Write unknown payees to an editable TSV; never infer a category."""
    groups = defaultdict(lambda: [0, 0, Decimal(0)])
    for _, lines, header in _entries(journal_dir):
        if result := _unknown(lines):
            _, _, direction, amount = result
            row = groups[header.group(2)]
            row[0 if direction == "debit" else 1] += 1
            if amount is not None:
                row[2] += amount
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["payee", "debits", "credits", "total_inr_debits",
                         "suggestion", "category"])
        for payee, (debits, credits, total) in sorted(
            groups.items(), key=lambda item: (-item[1][2], -sum(item[1][:2]), item[0])
        ):
            writer.writerow([payee, debits, credits, f"{total:.2f}",
                             suggest_category(payee), ""])
    return len(groups)


def apply_review(review_path: Path, mappings_path: Path) -> int:
    """Append approved payee categories to mappings.toml atomically."""
    text = Path(mappings_path).read_text()
    mappings = Mappings.loads(text)
    additions = {}
    with Path(review_path).open(newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            payee, category = row["payee"].strip(), row["category"].strip()
            if not category:
                continue
            if not payee or not ACCOUNT_RE.fullmatch(category):
                raise ValueError(f"invalid payee/category in review row: {payee!r}, {category!r}")
            if payee in mappings.payees:
                if mappings.payees[payee] != category:
                    raise ValueError(f"{payee!r} already has a different mapping")
                continue
            if payee in additions and additions[payee] != category:
                raise ValueError(f"conflicting categories for {payee!r}")
            additions[payee] = category
    if not additions:
        return 0
    section = re.search(r"(?m)^\[payees\]\s*$", text)
    if not section:
        raise ValueError("mappings.toml has no [payees] section")
    next_section = re.search(r"(?m)^\[.+\]\s*$", text[section.end():])
    insert_at = section.end() + next_section.start() if next_section else len(text)
    new_lines = "".join(f"{json.dumps(k)} = {json.dumps(v)}\n" for k, v in sorted(additions.items()))
    updated = text[:insert_at].rstrip("\n") + "\n" + new_lines + text[insert_at:]
    Mappings.loads(updated)  # Validate before replacing the live file.
    backup_dir = Path(mappings_path).parent / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir.mkdir(parents=True)
    shutil.copy2(mappings_path, backup_dir / Path(mappings_path).name)
    _atomic_write(Path(mappings_path), updated)
    return len(additions)


def timestamp_index(log_path: Path | None = None, db_path: Path | None = None):
    """Recover SMS receipt times by transaction key, from the log and/or Mac."""
    times = {}
    if log_path and Path(log_path).exists():
        for line in Path(log_path).read_text().splitlines():
            record = json.loads(line)
            payload = record.get("payload", record)
            stamp = payload.get("received_at") or record.get("at")
            if not stamp:
                continue
            received_at = datetime.fromisoformat(stamp)
            txn = parse(payload["sender"], payload["text"], received_at.date())
            if txn:
                times[dedup_key(txn)] = received_at
    if db_path:
        for stamp, sender, body in bank_sms(db_path):
            received_at = datetime.fromisoformat(stamp)
            txn = parse(sender, body, received_at.date())
            if txn:
                times[dedup_key(txn)] = received_at
    return times


def recategorize(journal_dir: Path, mappings: Mappings, times: dict[str, datetime],
                 *, apply: bool = False) -> tuple[int, int]:
    """Reclassify only unknown postings. Preserve existing entries and tags."""
    with journal_lock(Path(journal_dir)):
        return _recategorize_locked(journal_dir, mappings, times, apply=apply)


def _recategorize_locked(journal_dir: Path, mappings: Mappings, times: dict[str, datetime],
                         *, apply: bool) -> tuple[int, int]:
    changed_entries = tagged_entries = 0
    updates = {}
    for path in sorted(Path(journal_dir).glob("[0-9][0-9][0-9][0-9].journal")):
        original = path.read_text()
        blocks = original.split("\n\n")
        changed_file = False
        for index, block in enumerate(blocks):
            changed_block = False
            lines = block.splitlines()
            if len(lines) != 3 or not (header := HEADER_RE.match(lines[0])):
                continue
            key_match = KEY_RE.search(header.group(3))
            received_at = times.get(key_match.group(1)) if key_match else None
            tagged = TIME_RE.search(header.group(3))
            if tagged:
                received_at = datetime.combine(date.fromisoformat(header.group(1)),
                                               time.fromisoformat(tagged.group(1)))
            elif received_at:
                lines[0] += f", time:{received_at:%H:%M}"
                tagged_entries += 1
                changed_block = True
            if result := _unknown(lines):
                line_index, match, direction, amount = result
                category = mappings.target_account(header.group(2), direction,
                                                   amount=amount, received_at=received_at)
                if category not in {"expenses:unknown", "income:unknown"}:
                    lines[line_index] = match.group(1) + category + (match.group(3) or "")
                    changed_entries += 1
                    changed_block = True
            if changed_block:
                blocks[index] = "\n".join(lines) + ("\n" if block.endswith("\n") else "")
                changed_file = True
        if changed_file:
            updates[path] = "\n\n".join(blocks)
    if apply and updates:
        backup_dir = Path(journal_dir).parent / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_dir.mkdir(parents=True)
        for path in updates:
            shutil.copy2(path, backup_dir / path.name)
        for path, content in updates.items():
            _atomic_write(path, content)
    return changed_entries, tagged_entries


def _atomic_write(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, path.stat().st_mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
