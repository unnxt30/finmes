#!/usr/bin/env python3
"""Extract bank/UPI SMS from the macOS Messages database into a TSV corpus.

Usage: python3 scripts/extract_bank_sms.py [output_path] [db_path]

Reading ~/Library/Messages/chat.db requires Full Disk Access for the terminal.
"""

import sys
from pathlib import Path

# Allow direct execution from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finmes.messages import CHAT_DB, bank_sms  # noqa: E402


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples/bank_sms.txt")
    db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else CHAT_DB
    samples = list(bank_sms(db_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(f"{ts}\t{sender}\t{body}\n" for ts, sender, body in samples))

    print(f"{len(samples)} transaction messages -> {out_path}")
    senders = {}
    for _, sender, _ in samples:
        senders[sender] = senders.get(sender, 0) + 1
    for sender, count in sorted(senders.items(), key=lambda kv: -kv[1]):
        print(f"  {sender}: {count}")


if __name__ == "__main__":
    main()
