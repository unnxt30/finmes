import json
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from finmes.cli import main
from finmes.remote import RemoteRecapError, remote_recap
from finmes.server import make_server

RAW = ("ICICI Bank Acct XX973 debited for Rs 53.00 on 22-Jul-26; TESTPAYEE "
       "credited. UPI:163451439277. Call 18002662 for dispute.")
AT = datetime(2026, 7, 22, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
RECORD = ("ICICIT-S", RAW, AT.date(), None, AT)


@pytest.fixture
def remote(tmp_path):
    journal = tmp_path / "remote-journal"
    journal.mkdir()
    mappings = tmp_path / "remote-mappings.toml"
    mappings.write_text('[accounts]\nXX973 = "assets:bank:icici"\n')
    log = tmp_path / "remote-log.jsonl"
    server = make_server("127.0.0.1", 0, journal_dir=journal,
                         mappings_path=mappings, log_path=log, token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/ingest", journal, log
    server.shutdown()
    server.server_close()
    thread.join()


def test_remote_cli_uses_vps_and_is_safe_to_repeat(remote, tmp_path, monkeypatch, capsys):
    url, journal, log = remote
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FINMES_INGEST_TOKEN", raising=False)
    (tmp_path / ".env").write_text("FINMES_INGEST_TOKEN='secret'\n")
    monkeypatch.setattr("finmes.cli._read_messages", lambda *args: iter([RECORD]))
    args = ["recap", "2026-07-22", "2026-07-22", "--url", url]
    assert main(args) == 0
    assert "VPS added: 1" in capsys.readouterr().out
    assert not (tmp_path / "data").exists()
    assert main(args) == 0
    assert "VPS added: 0  duplicate: 1" in capsys.readouterr().out
    assert (journal / "2026.journal").read_text().count("key:163451439277") == 1
    sent = json.loads(log.read_text().splitlines()[0])["payload"]
    assert datetime.fromisoformat(sent["received_at"]).timestamp() == AT.timestamp()
    assert datetime.fromisoformat(sent["received_at"]).tzinfo is not None


def test_bad_token_does_not_write_remote(remote):
    url, journal, log = remote
    with pytest.raises(RemoteRecapError, match="HTTP 401"):
        remote_recap([RECORD], url, "wrong")
    assert not log.exists()
    assert not list(journal.glob("*.journal"))


def test_remote_refuses_insecure_nonlocal_url():
    with pytest.raises(RemoteRecapError, match="HTTPS"):
        remote_recap([], "http://example.com/ingest", "secret")


def test_remote_does_not_follow_redirects():
    calls = []
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_response(307)
            self.send_header("Location", "/other")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(RemoteRecapError, match="HTTP 307"):
            remote_recap([RECORD], f"http://127.0.0.1:{server.server_port}/ingest", "secret")
        assert calls == ["/ingest"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
