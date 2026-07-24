#!/usr/bin/env python3
"""Throwaway /ingest stub for testing the iOS automation end-to-end.

Validates the bearer token, runs the real parser (READ-ONLY — never touches
the journal), logs every request to ingest_stub.jsonl, and returns the real
response contract. Replaced by the actual FastAPI endpoint later.

Usage: .venv/bin/python scripts/ingest_stub.py  (listens on 0.0.0.0:8643)
"""

import json
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from finmes.parser import SKIP_RE, parse

REPO = Path(__file__).resolve().parent.parent
TOKEN = next(
    line.split("=", 1)[1].strip()
    for line in (REPO / ".env").read_text().splitlines()
    if line.startswith("FINMES_INGEST_TOKEN=")
)
LOG = REPO / "ingest_stub.jsonl"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "note": "finmes ingest stub"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/ingest":
            self._send(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"error": "bad token"})
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text, sender = payload["text"], payload["sender"]
        except (KeyError, ValueError, TypeError) as e:
            self._send(400, {"error": f"bad payload: {e}"})
            return

        received = None
        if payload.get("received_at"):
            received = datetime.fromisoformat(payload["received_at"]).date()
        txn = parse(sender, text, received=received or date.today())
        if txn:
            status = "parsed"
            summary = f"TEST: {txn.currency} {txn.amount} {txn.direction}, {txn.counterparty or '?'}"
        elif SKIP_RE.search(text):
            status, summary = "skipped", "TEST: non-transaction SMS, ignored"
        else:
            status, summary = "unparsed", "TEST: could not parse, would go to review"

        with LOG.open("a") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(), "payload": payload, "status": status}) + "\n")
        print(f"{datetime.now():%H:%M:%S} {status}: [{sender}] {text[:80]}", flush=True)
        self._send(200, {"status": status, "summary": summary, "duplicate": False})


if __name__ == "__main__":
    print(f"ingest stub on 0.0.0.0:8643, logging to {LOG}", flush=True)
    HTTPServer(("0.0.0.0", 8643), Handler).serve_forever()
