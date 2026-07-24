"""finmes CLI. Currently: backfill. Later: summary, unknowns, reconcile."""

import argparse
from datetime import date
from pathlib import Path

from finmes.journal import Mappings, ingest


def _read_corpus(path: Path):
    """Yield (sender, body, received) from the TSV corpus: <ts>\\t<sender>\\t<body>."""
    for line in path.read_text().splitlines():
        ts, sender, body = line.split("\t", 2)
        yield sender, body, date.fromisoformat(ts[:10]), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finmes")
    sub = parser.add_subparsers(dest="command", required=True)
    bf = sub.add_parser("backfill", help="ingest an SMS corpus TSV into the journal")
    bf.add_argument("corpus", type=Path, nargs="?", default=Path("samples/bank_sms.txt"))
    bf.add_argument("--journal-dir", type=Path, default=Path("data/journal"))
    bf.add_argument("--mappings", type=Path, default=Path("data/mappings.toml"))
    args = parser.parse_args(argv)

    mappings = Mappings.load(args.mappings)
    summary = ingest(_read_corpus(args.corpus), args.journal_dir, mappings)
    print(f"added: {summary.added}  duplicate: {summary.duplicate}  unparsed/skipped: {summary.unparsed}")
    if summary.unknown_payees:
        print(f"unknown payees ({len(summary.unknown_payees)}):")
        for payee in sorted(summary.unknown_payees):
            print(f"  {payee}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
