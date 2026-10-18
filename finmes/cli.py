"""finmes CLI: backfill, replay, recap. Later: summary, unknowns, reconcile."""

import argparse
import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from finmes.categories import apply_review, recategorize, review_unknowns, timestamp_index
from finmes.journal import Mappings, ingest
from finmes.messages import CHAT_DB, bank_sms
from finmes.remote import RemoteRecapError, remote_recap


def _read_corpus(path: Path):
    """Yield SMS records from the TSV corpus: <ts>\\t<sender>\\t<body>."""
    for line in path.read_text().splitlines():
        ts, sender, body = line.split("\t", 2)
        yield sender, body, date.fromisoformat(ts[:10]), None, datetime.fromisoformat(ts)


def _read_ingest_log(path: Path):
    """Yield (sender, body, received, location) from the raw JSONL ingest log.
    Every logged payload goes back through parse() — replay decides, not the
    logged status (the parser may have improved since the message arrived)."""
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        p = rec.get("payload", rec)
        if p.get("received_at"):
            received_at = datetime.fromisoformat(p["received_at"])
        else:
            received_at = datetime.fromisoformat(rec["at"])
        yield p["sender"], p["text"], received_at.date(), p.get("location"), received_at


def _read_messages(db_path: Path, start: date, end: date):
    for timestamp, sender, body in bank_sms(db_path, start, end):
        yield sender, body, date.fromisoformat(timestamp[:10]), None, datetime.fromisoformat(timestamp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finmes")
    sub = parser.add_subparsers(dest="command", required=True)
    bf = sub.add_parser("backfill", help="ingest an SMS corpus TSV into the journal")
    bf.add_argument("corpus", type=Path, nargs="?", default=Path("samples/bank_sms.txt"))
    bf.add_argument("--journal-dir", type=Path, default=Path("data/journal"))
    bf.add_argument("--mappings", type=Path, default=Path("data/mappings.toml"))

    rp = sub.add_parser("replay", help="re-ingest the raw JSONL ingest log (idempotent)")
    rp.add_argument("log", type=Path, nargs="?", default=Path("data/ingest.jsonl"))
    rp.add_argument("--journal-dir", type=Path, default=Path("data/journal"))
    rp.add_argument("--mappings", type=Path, default=Path("data/mappings.toml"))

    rc = sub.add_parser("recap", help="catch up from synced Mac Messages for an inclusive date range")
    rc.add_argument("start", type=date.fromisoformat, help="first received date (YYYY-MM-DD)")
    rc.add_argument("end", type=date.fromisoformat, help="last received date (YYYY-MM-DD)")
    rc.add_argument("--db", type=Path, default=CHAT_DB, help="Messages chat.db path")
    rc.add_argument("--journal-dir", type=Path, default=Path("data/journal"))
    rc.add_argument("--mappings", type=Path, default=Path("data/mappings.toml"))
    rc.add_argument("--url", help="POST recovered messages to this VPS ingest URL instead of writing locally")
    rc.add_argument("--env-file", type=Path, default=Path(".env"),
                    help="fallback source of FINMES_INGEST_TOKEN (environment takes precedence)")

    review = sub.add_parser("review", help="export uncategorized payees for batch review")
    review.add_argument("--journal-dir", type=Path, default=Path("data/journal"))
    review.add_argument("--output", type=Path, default=Path("data/payee-review.tsv"))

    apply_categories = sub.add_parser("apply-review", help="save filled categories and update old entries")
    apply_categories.add_argument("review", type=Path, nargs="?", default=Path("data/payee-review.tsv"))
    apply_categories.add_argument("--mappings", type=Path, default=Path("data/mappings.toml"))
    apply_categories.add_argument("--journal-dir", type=Path, default=Path("data/journal"))

    recat = sub.add_parser("recategorize", help="update old unknown entries from mappings and receipt times")
    recat.add_argument("--journal-dir", type=Path, default=Path("data/journal"))
    recat.add_argument("--mappings", type=Path, default=Path("data/mappings.toml"))
    recat.add_argument("--log", type=Path, default=Path("data/ingest.jsonl"))
    recat.add_argument("--db", type=Path, help="optional Mac Messages database for historical receipt times")
    recat.add_argument("--apply", action="store_true", help="write the changes (default: preview only)")

    args = parser.parse_args(argv)
    if args.command == "recap" and args.start > args.end:
        parser.error("recap start date must be on or before end date")
    if args.command == "recap" and args.url:
        token = os.environ.get("FINMES_INGEST_TOKEN", "")
        if not token and args.env_file.exists():
            for line in args.env_file.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "FINMES_INGEST_TOKEN":
                    token = value.strip()
                    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
                        token = token[1:-1]
        try:
            summary = remote_recap(_read_messages(args.db, args.start, args.end), args.url, token)
        except sqlite3.OperationalError:
            parser.exit(1, "cannot read Messages database. Grant this terminal Full Disk Access.\n")
        except RemoteRecapError as exc:
            parser.exit(1, f"{exc}\n")
        print(f"VPS added: {summary.added}  duplicate: {summary.duplicate}  unparsed/skipped: {summary.unparsed}")
        return 0
    if args.command == "review":
        count = review_unknowns(args.journal_dir, args.output)
        print(f"{count} uncategorized payees -> {args.output}")
        return 0
    if args.command == "apply-review":
        count = apply_review(args.review, args.mappings)
        changed, _ = recategorize(args.journal_dir, Mappings.load(args.mappings), {}, apply=True)
        print(f"mapped: {count} payees  updated: {changed} old entries")
        return 0
    if args.command == "recategorize":
        mappings = Mappings.load(args.mappings)
        try:
            times = timestamp_index(args.log, args.db)
        except sqlite3.OperationalError as exc:
            parser.exit(1, f"cannot read Messages database: {exc}. "
                           "Grant this terminal Full Disk Access.\n")
        changed, tagged = recategorize(args.journal_dir, mappings, times, apply=args.apply)
        mode = "updated" if args.apply else "would update"
        print(f"{mode}: {changed} categories, {tagged} receipt-time tags")
        return 0

    mappings = Mappings.load(args.mappings)
    if args.command == "backfill":
        records = _read_corpus(args.corpus)
    elif args.command == "replay":
        records = _read_ingest_log(args.log)
    else:
        records = _read_messages(args.db, args.start, args.end)
    try:
        summary = ingest(records, args.journal_dir, mappings)
    except sqlite3.OperationalError as exc:
        if args.command != "recap":
            raise
        parser.exit(1, f"cannot read Messages database at {args.db}: {exc}. "
                       "Check the path and grant this terminal Full Disk Access.\n")
    print(f"added: {summary.added}  duplicate: {summary.duplicate}  unparsed/skipped: {summary.unparsed}")
    if summary.unknown_payees:
        print(f"unknown payees ({len(summary.unknown_payees)}):")
        for payee in sorted(summary.unknown_payees):
            print(f"  {payee}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
