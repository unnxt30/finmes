"""Ingest endpoint: the phone forwards every SMS here; all filtering lives
server-side (dumb client, smart server).

Financial-looking messages are appended to the raw JSONL log (source of
truth, written before the journal) and run through the deterministic
parser -> journal path. Everything else returns 200 "ignored" and is never
persisted, so personal messages don't accumulate on disk.
"""

import json
import os
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from finmes.journal import Mappings, dedup_key, existing_keys
from finmes.journal import ingest as journal_ingest
from finmes.parser import SKIP_RE, parse

# Logging gate, not a parse gate: broad on purpose. Anything remotely
# bank-shaped is kept for replay; only clearly-personal messages are dropped.
FINANCIAL_RE = re.compile(
    r"₹|\bRs\.?\s?\d|\bINR\b|debit|credit|\bA/c\b|\bAcct\b|\baccount\b|\bcard\b"
    r"|\bUPI\b|\bIMPS\b|\bNEFT\b|\bNACH\b|\bOTP\b|\bbank\b|\bEMI\b|autopay"
    r"|payment|spent|withdraw|deposit|\bbalance\b|statement|mandate",
    re.IGNORECASE,
)


def handle(payload: dict, journal_dir: Path, mappings: Mappings, log_path: Path) -> dict:
    """Process one ingest payload; returns the {status, summary, duplicate} contract."""
    text, sender = payload["text"], payload["sender"]
    received = date.today()
    if payload.get("received_at"):
        received = datetime.fromisoformat(payload["received_at"]).date()

    txn = parse(sender, text, received=received)
    if txn is None and not FINANCIAL_RE.search(text):
        return {"status": "ignored", "summary": "not a bank SMS, dropped", "duplicate": False}

    duplicate = False
    if txn:
        status = "parsed"
        duplicate = dedup_key(txn) in existing_keys(journal_dir)
        amt = f"{'₹' if txn.currency == 'INR' else txn.currency + ' '}{txn.amount}"
        summary = f"{amt} {txn.direction}, {txn.counterparty or '?'}"
        if duplicate:
            summary += " (duplicate)"
    elif SKIP_RE.search(text):
        status, summary = "skipped", "non-transaction SMS, ignored"
    else:
        status, summary = "unparsed", "could not parse, kept for review"

    # Raw log first: if the journal append fails, the SMS is still replayable.
    record = {"at": datetime.now().isoformat(), "payload": payload, "status": status,
              "duplicate": duplicate}
    if txn:
        record["key"] = dedup_key(txn)
    with Path(log_path).open("a") as f:
        f.write(json.dumps(record) + "\n")

    if txn and not duplicate:
        journal_ingest([(sender, text, received, payload.get("location"))],
                       journal_dir, mappings)

    return {"status": status, "summary": summary, "duplicate": duplicate}


class _Handler(BaseHTTPRequestHandler):
    # injected by make_server
    journal_dir: Path
    mappings_path: Path
    log_path: Path
    token: str

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/ingest":
            self._send(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._send(401, {"error": "bad token"})
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            # Reload per request so mapping edits apply without a restart.
            mappings = Mappings.load(self.mappings_path)
            resp = handle(payload, self.journal_dir, mappings, self.log_path)
        except (KeyError, ValueError, TypeError) as e:
            self._send(400, {"error": f"bad payload: {e}"})
            return
        print(f"{datetime.now():%H:%M:%S} {resp['status']}: {payload.get('text', '')[:80]}", flush=True)
        self._send(200, resp)


def make_server(host: str, port: int, *, journal_dir: Path, mappings_path: Path,
                log_path: Path, token: str) -> HTTPServer:
    handler = type("Handler", (_Handler,), dict(
        journal_dir=Path(journal_dir), mappings_path=Path(mappings_path),
        log_path=Path(log_path), token=token,
    ))
    return HTTPServer((host, port), handler)


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    token = os.environ.get("FINMES_INGEST_TOKEN")
    if not token:
        token = next(
            line.split("=", 1)[1].strip()
            for line in (repo / ".env").read_text().splitlines()
            if line.startswith("FINMES_INGEST_TOKEN=")
        )
    srv = make_server(
        os.environ.get("FINMES_HOST", "0.0.0.0"),
        int(os.environ.get("FINMES_PORT", "8643")),
        journal_dir=Path(os.environ.get("FINMES_JOURNAL_DIR", repo / "data/journal")),
        mappings_path=Path(os.environ.get("FINMES_MAPPINGS", repo / "data/mappings.toml")),
        log_path=Path(os.environ.get("FINMES_INGEST_LOG", repo / "data/ingest.jsonl")),
        token=token,
    )
    print(f"finmes ingest on {srv.server_address[0]}:{srv.server_address[1]}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
