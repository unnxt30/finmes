"""Tests for the ingest endpoint: POST payload -> JSONL log + journal append.

The phone forwards every SMS; all filtering lives here. Financial-looking
messages are logged (raw = source of truth) and parsed; everything else is
dropped without being persisted.
"""

import json
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from finmes.journal import Mappings, existing_keys
from finmes.server import handle, make_server

ICICI_SMS = (
    "ICICI Bank Acct XX973 debited for Rs 165.00 on 29-Jun-26; "
    "ZEPTOMARKETPLAC credited. UPI:203007126186."
)

MAPPINGS_TOML = """\
[accounts]
XX973 = "assets:bank:icici"

[senders]
SBICRD = "liabilities:sbi-card"

[markers]
"SBI Credit Card" = "liabilities:sbi-card"

[payees]
ZEPTOMARKETPLAC = "expenses:groceries"
"""


def payload(**kw: Any) -> dict:
    base: dict[str, Any] = {
        "text": ICICI_SMS,
        "sender": "ios",
        "received_at": "2026-07-22T14:03:21+05:30",
        "location": {"lat": "28.65358897706612", "lon": "77.44984089225419"},
    }
    base.update(kw)
    return base


@pytest.fixture
def env(tmp_path):
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    log = tmp_path / "ingest.jsonl"
    mappings = Mappings.loads(MAPPINGS_TOML)
    return journal_dir, mappings, log


class TestHandle:
    def test_mac_metadata_files_do_not_block_ingestion(self, env):
        journal_dir, mappings, log = env
        (journal_dir / "._2026.journal").write_bytes(b"\x00\x05\x16\x07\xff")
        (journal_dir / "directory.journal").mkdir()
        first = handle(payload(), journal_dir, mappings, log)
        second = handle(payload(), journal_dir, mappings, log)
        assert first["status"] == "parsed" and first["duplicate"] is False
        assert second["status"] == "parsed" and second["duplicate"] is True
        assert len(existing_keys(journal_dir)) == 1
        assert len(log.read_text().splitlines()) == 2

    def test_parsed_sms_appends_journal_entry(self, env):
        journal_dir, mappings, log = env
        resp = handle(payload(), journal_dir, mappings, log)
        assert resp["status"] == "parsed"
        assert resp["duplicate"] is False
        assert "165.00" in resp["summary"]
        entry = (journal_dir / "2026.journal").read_text()
        assert "2026-06-29 ZEPTOMARKETPLAC" in entry
        assert "expenses:groceries" in entry
        assert "assets:bank:icici" in entry

    def test_second_post_is_duplicate(self, env):
        journal_dir, mappings, log = env
        handle(payload(), journal_dir, mappings, log)
        resp = handle(payload(), journal_dir, mappings, log)
        assert resp["status"] == "parsed"
        assert resp["duplicate"] is True
        assert len(existing_keys(journal_dir)) == 1

    def test_financial_sms_logged_to_jsonl(self, env):
        journal_dir, mappings, log = env
        handle(payload(), journal_dir, mappings, log)
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["payload"] == payload()  # raw payload verbatim
        assert records[0]["status"] == "parsed"

    def test_otp_skipped_but_logged(self, env):
        journal_dir, mappings, log = env
        resp = handle(
            payload(text="OTP is 123456 for txn of Rs 500 at Amazon"),
            journal_dir, mappings, log,
        )
        assert resp["status"] == "skipped"
        assert not existing_keys(journal_dir)
        assert json.loads(log.read_text())["status"] == "skipped"

    def test_unmatched_financial_sms_unparsed_and_logged(self, env):
        journal_dir, mappings, log = env
        resp = handle(
            payload(text="Rs 942.00 spent via some new bank SMS template we have never seen"),
            journal_dir, mappings, log,
        )
        assert resp["status"] == "unparsed"
        assert not existing_keys(journal_dir)
        assert json.loads(log.read_text())["status"] == "unparsed"

    def test_personal_message_ignored_and_never_persisted(self, env):
        journal_dir, mappings, log = env
        resp = handle(
            payload(text="hey, are we still on for dinner tomorrow?"),
            journal_dir, mappings, log,
        )
        assert resp["status"] == "ignored"
        assert not log.exists()
        assert not existing_keys(journal_dir)

    def test_missing_received_at_still_parses(self, env):
        journal_dir, mappings, log = env
        resp = handle(payload(received_at=None), journal_dir, mappings, log)
        assert resp["status"] == "parsed"

    def test_missing_text_raises(self, env):
        journal_dir, mappings, log = env
        with pytest.raises((KeyError, ValueError, TypeError)):
            handle({"sender": "ios"}, journal_dir, mappings, log)

    def test_parsed_entry_gets_location_tags(self, env):
        journal_dir, mappings, log = env
        handle(payload(), journal_dir, mappings, log)
        entry = (journal_dir / "2026.journal").read_text()
        assert "lat:28.65359, lon:77.44984" in entry

    def test_jsonl_record_carries_dedup_key(self, env):
        journal_dir, mappings, log = env
        handle(payload(), journal_dir, mappings, log)
        rec = json.loads(log.read_text())
        assert rec["key"] == "203007126186.XX973.debit"

    def test_skipped_record_has_no_key(self, env):
        journal_dir, mappings, log = env
        handle(payload(text="OTP is 123456 for txn of Rs 500 at Amazon"),
               journal_dir, mappings, log)
        assert "key" not in json.loads(log.read_text())


class TestHTTP:
    @pytest.fixture
    def server(self, tmp_path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        mappings_path = tmp_path / "mappings.toml"
        mappings_path.write_text(MAPPINGS_TOML)
        srv = make_server(
            "127.0.0.1", 0,
            journal_dir=journal_dir,
            mappings_path=mappings_path,
            log_path=tmp_path / "ingest.jsonl",
            token="sekrit",
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{srv.server_address[1]}"
        srv.shutdown()

    def _post(self, url: str, body: dict, token: str) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{url}/ingest",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_bad_token_401(self, server):
        code, _ = self._post(server, payload(), token="wrong")
        assert code == 401

    def test_parsed_roundtrip(self, server):
        code, body = self._post(server, payload(), token="sekrit")
        assert code == 200
        assert body["status"] == "parsed"
        assert body["duplicate"] is False

    def test_malformed_payload_400(self, server):
        code, _ = self._post(server, {"sender": "ios"}, token="sekrit")
        assert code == 400

    def test_oversized_body_413(self, server):
        code, _ = self._post(server, payload(text="x" * 16384), token="sekrit")
        assert code == 413

    def test_health(self, server):
        with urllib.request.urlopen(f"{server}/health") as resp:
            assert resp.status == 200
