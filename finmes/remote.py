"""Send Mac Messages catch-up records through the authoritative ingest API."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from finmes.journal import Summary


class RemoteRecapError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def remote_recap(records, url: str, token: str) -> Summary:
    target = urlsplit(url)
    if target.username or target.password or target.fragment:
        raise RemoteRecapError("use an ingest URL without credentials or a fragment")
    if not target.hostname or (target.scheme != "https" and not (
        target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1", "::1"}
    )):
        raise RemoteRecapError("remote recap requires HTTPS (HTTP is allowed only on loopback)")
    if not token.strip():
        raise RemoteRecapError("FINMES_INGEST_TOKEN is missing")
    opener = build_opener(_NoRedirect())
    summary = Summary()
    for sender, body, received, location, received_at in records:
        payload = {"sender": sender, "text": body,
                   "received_at": received_at.astimezone().isoformat()}
        if location is not None:
            payload["location"] = location
        req = Request(url, data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        })
        try:
            with opener.open(req, timeout=30) as response:
                result = json.load(response)
            if result["status"] == "parsed":
                if not isinstance(result.get("duplicate"), bool):
                    raise ValueError("invalid duplicate flag")
                if result["duplicate"]:
                    summary.duplicate += 1
                else:
                    summary.added += 1
            elif result["status"] in {"skipped", "unparsed", "ignored"}:
                summary.unparsed += 1
            else:
                raise ValueError("invalid ingest status")
        except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError) as exc:
            reason = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
            raise RemoteRecapError(
                f"upload stopped ({reason}); confirmed added: {summary.added}, "
                f"duplicate: {summary.duplicate}, skipped: {summary.unparsed}. "
                "Rerun the same date range safely after fixing the connection/token."
            ) from exc
    return summary
