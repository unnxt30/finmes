#!/usr/bin/env python3
"""Run the parser over samples/bank_sms.txt and report coverage.

Usage: .venv/bin/python scripts/parse_corpus.py [samples_path] [--unmatched]
"""

import sys
from collections import Counter
from datetime import date
from pathlib import Path

from finmes.parser import SKIP_RE, parse

BANK_SENDERS = ("ICICIT", "ICICIO", "SBICRD", "SBIUPI", "SBIINB", "MYSBIC", "SBIBNK", "SBYONO", "CBSSBI", "IOBCHN", "HDFCBK", "AXISBK", "JUSPAY")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_unmatched = "--unmatched" in sys.argv
    path = Path(args[0]) if args else Path("samples/bank_sms.txt")

    parsed, skipped = 0, 0
    unmatched_bank: list[tuple[str, str]] = []
    unmatched_other = 0
    channels: Counter[str] = Counter()

    for line in path.read_text().splitlines():
        ts, sender, body = line.split("\t", 2)
        txn = parse(sender, body, received=date.fromisoformat(ts[:10]))
        if txn:
            parsed += 1
            channels[f"{txn.direction}/{txn.channel}"] += 1
        elif SKIP_RE.search(body):
            skipped += 1
        elif any(b in sender for b in BANK_SENDERS):
            unmatched_bank.append((sender, body))
        else:
            unmatched_other += 1

    total = parsed + skipped + len(unmatched_bank) + unmatched_other
    print(f"total: {total}  parsed: {parsed}  skipped (OTP/etc): {skipped}")
    print(f"unmatched bank messages: {len(unmatched_bank)}  non-bank noise: {unmatched_other}\n")
    for key, count in channels.most_common():
        print(f"  {key}: {count}")

    if show_unmatched:
        print("\n--- unmatched bank messages ---")
        for sender, body in unmatched_bank:
            print(f"[{sender}] {body[:160]}")


if __name__ == "__main__":
    main()
