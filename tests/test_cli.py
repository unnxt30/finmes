"""Tests for finmes CLI: replay of the raw ingest log."""

import json

from finmes.cli import main as cli_main

RAW = ("ICICI Bank Acct XX973 debited for Rs 1000.00 on 22-Jul-26; SANTUSHTI "
       "credited. UPI:163451439277. Call 18002662 for dispute.")

MAPPINGS = '[accounts]\nXX973 = "assets:bank:icici"\n'


def _write_log(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_replay_ingests_parsed_payloads_with_location(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    (tmp_path / "m.toml").write_text(MAPPINGS)
    log = tmp_path / "ingest.jsonl"
    _write_log(log, [
        {"at": "2026-07-22T16:35:00", "status": "parsed",
         "payload": {"text": RAW, "sender": "ios",
                     "received_at": "2026-07-22T16:34:59+05:30",
                     "location": {"lat": "28.6", "lon": "77.2"}}},
        {"at": "2026-07-22T23:53:06", "status": "unparsed",
         "payload": {"text": "", "sender": "ios"}},
    ])
    rc = cli_main(["replay", str(log), "--journal-dir", str(journal),
                   "--mappings", str(tmp_path / "m.toml")])
    assert rc == 0
    entry = (journal / "2026.journal").read_text()
    assert "2026-07-22 SANTUSHTI" in entry
    assert "lat:28.6, lon:77.2" in entry


def test_replay_is_idempotent(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    (tmp_path / "m.toml").write_text(MAPPINGS)
    log = tmp_path / "ingest.jsonl"
    _write_log(log, [{"at": "2026-07-22T16:35:00", "status": "parsed",
                      "payload": {"text": RAW, "sender": "ios"}}])
    args = ["replay", str(log), "--journal-dir", str(journal),
            "--mappings", str(tmp_path / "m.toml")]
    cli_main(args)
    cli_main(args)
    assert (journal / "2026.journal").read_text().count("SANTUSHTI") == 1
