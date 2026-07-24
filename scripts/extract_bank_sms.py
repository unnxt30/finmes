#!/usr/bin/env python3
"""Extract bank/UPI SMS from the macOS Messages database into a sample file.

Usage: python3 scripts/extract_bank_sms.py [output_path] [db_path]

Reads ~/Library/Messages/chat.db by default (requires Full Disk Access for the
terminal running it). Pass a copied chat.db as db_path to skip that requirement.
"""

import re
import sqlite3
import sys
from pathlib import Path

CHAT_DB = Path.home() / "Library/Messages/chat.db"

# Sender IDs in this DB look like "ICICIT-S(smsft_fi)", "SBIUPI-S(smsft_fi)",
# "57575022(smsft)" — alphanumeric ID, optional -X route suffix, iOS SMS-filter
# tag in parens. Anything that's not a phone number, basically.
SENDER_ID_RE = re.compile(r"^[A-Za-z0-9]{5,9}(-[A-Z])?(\([a-z_]+\))?$")

# Keywords that mark a transaction message (vs OTPs, promos)
TXN_KEYWORDS = re.compile(
    r"debited|credited|UPI|A/c|Acct|withdrawn|spent|received|txn|transaction|Rs\.?\s?\d|INR\s?\d",
    re.IGNORECASE,
)


def decode_attributed_body(blob: bytes) -> str | None:
    """Pull the plain string out of an archived NSAttributedString (typedstream).

    Heuristic decoder: the text is a length-prefixed UTF-8 string right after
    the NSString class marker. Good enough for plain SMS bodies.
    """
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx == -1:
        return None
    start = idx + len(b"NSString") + 5  # skip class header bytes
    if start >= len(blob):
        return None
    length = blob[start]
    if length == 0x81:  # two-byte little-endian length follows
        length = int.from_bytes(blob[start + 1 : start + 3], "little")
        start += 3
    else:
        start += 1
    return blob[start : start + length].decode("utf-8", errors="replace")


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples/bank_sms.txt")
    db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else CHAT_DB

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        """
        SELECT
            handle.id AS sender,
            message.text,
            message.attributedBody,
            datetime(message.date / 1000000000 + 978307200, 'unixepoch', 'localtime') AS ts
        FROM message
        JOIN handle ON message.handle_id = handle.ROWID
        WHERE message.is_from_me = 0
        ORDER BY message.date
        """
    ).fetchall()
    conn.close()

    samples = []
    senders = {}
    for sender, text, blob, ts in rows:
        if not SENDER_ID_RE.match(sender):
            continue
        body = text or decode_attributed_body(blob)
        if not body or not TXN_KEYWORDS.search(body):
            continue
        body = " ".join(body.split())  # collapse newlines/whitespace
        samples.append(f"{ts}\t{sender}\t{body}")
        senders[sender] = senders.get(sender, 0) + 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(samples) + "\n")

    print(f"{len(samples)} transaction messages -> {out_path}")
    for sender, count in sorted(senders.items(), key=lambda kv: -kv[1]):
        print(f"  {sender}: {count}")


if __name__ == "__main__":
    main()
