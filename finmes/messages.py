"""Read transaction-shaped SMS from the local macOS Messages database."""

import re
import sqlite3
from datetime import date
from pathlib import Path

CHAT_DB = Path.home() / "Library/Messages/chat.db"

# Sender IDs in Messages may include an SMS-filter suffix.
SENDER_ID_RE = re.compile(r"^[A-Za-z0-9]{5,9}(-[A-Z])?(\([a-z_]+\))?$")
TXN_KEYWORDS = re.compile(
    r"debited|credited|UPI|A/c|Acct|withdrawn|spent|received|txn|transaction|Rs\.?\s?\d|INR\s?\d",
    re.IGNORECASE,
)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract plain text from a Messages archived NSAttributedString."""
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx == -1:
        return None
    start = idx + len(b"NSString") + 5
    if start >= len(blob):
        return None
    length = blob[start]
    if length == 0x81:
        length = int.from_bytes(blob[start + 1 : start + 3], "little")
        start += 3
    else:
        start += 1
    return blob[start : start + length].decode("utf-8", errors="replace")


def bank_sms(db_path: Path = CHAT_DB, start: date | None = None,
             end: date | None = None):
    """Yield (local timestamp, sender, body) for received bank-like SMS.

    The range covers the Messages receipt date, inclusive, in the Mac's local
    time zone. The database is opened read-only and is never changed.
    """
    if start and end and start > end:
        raise ValueError("start date must be on or before end date")
    db_uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        where = ["message.is_from_me = 0"]
        params = []
        local_day = "date(message.date / 1000000000 + 978307200, 'unixepoch', 'localtime')"
        if start:
            where.append(f"{local_day} >= ?")
            params.append(start.isoformat())
        if end:
            where.append(f"{local_day} <= ?")
            params.append(end.isoformat())
        rows = conn.execute(
            f"""SELECT handle.id AS sender, message.text, message.attributedBody,
                       datetime(message.date / 1000000000 + 978307200,
                                'unixepoch', 'localtime') AS ts
                FROM message
                JOIN handle ON message.handle_id = handle.ROWID
                WHERE {' AND '.join(where)}
                ORDER BY message.date""",
            params,
        )
        for sender, text, blob, ts in rows:
            if not sender or not SENDER_ID_RE.match(sender):
                continue
            body = text or decode_attributed_body(blob)
            if not body or not TXN_KEYWORDS.search(body):
                continue
            yield ts, sender, " ".join(body.split())
    finally:
        conn.close()
