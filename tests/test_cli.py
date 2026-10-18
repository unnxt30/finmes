"""Tests for finmes CLI: replay and Mac Messages recap."""

import json
import sqlite3
from datetime import datetime

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


def test_recap_reads_only_requested_days_and_skips_existing_entries(tmp_path, capsys):
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (handle_id INTEGER, text TEXT, attributedBody BLOB,
                              date INTEGER, is_from_me INTEGER);
        INSERT INTO handle VALUES (1, 'ICICIT-S(smsft_fi)');
    """)
    def apple_ns(day):
        dt = datetime.fromisoformat(day + "T12:00:00+00:00")
        return int((dt.timestamp() - 978307200) * 1_000_000_000)
    conn.executemany("INSERT INTO message VALUES (1, ?, NULL, ?, 0)", [
        (RAW, apple_ns("2026-07-22")),
        (RAW.replace("163451439277", "163451439278"), apple_ns("2026-07-23")),
        (RAW.replace("163451439277", "163451439279"), apple_ns("2026-07-24")),
    ])
    conn.commit()
    conn.close()

    journal = tmp_path / "journal"
    journal.mkdir()
    mappings = tmp_path / "mappings.toml"
    mappings.write_text(MAPPINGS)
    args = ["recap", "2026-07-22", "2026-07-23", "--db", str(db),
            "--journal-dir", str(journal), "--mappings", str(mappings)]
    assert cli_main(args) == 0
    assert "added: 2" in capsys.readouterr().out
    entry = (journal / "2026.journal").read_text()
    assert "163451439277" in entry and "163451439278" in entry
    assert "163451439279" not in entry
    assert cli_main(args) == 0
    assert "added: 0  duplicate: 2" in capsys.readouterr().out
