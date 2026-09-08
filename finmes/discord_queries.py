"""Run fixed hledger commands and format output for Discord."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_PERIOD_RE = re.compile(r"^(this|last)[a-z0-9 ]+$|^20\d\d(-\d\d)?$")
_CATEGORY_RE = re.compile(r"^[a-z:]+$")


class HledgerError(RuntimeError):
    """Raised when hledger exits with a nonzero status."""


def _validate_period(period: str) -> None:
    if not _PERIOD_RE.fullmatch(period):
        raise ValueError(f"invalid period: {period!r}")


def _validate_category(category: str) -> None:
    if not _CATEGORY_RE.fullmatch(category):
        raise ValueError(f"invalid category: {category!r}")


def _sanitize_stderr(stderr: str, ledger_file: Path) -> str:
    text = stderr.strip().replace(str(ledger_file), "<ledger>")
    return text or "hledger command failed"


def run_hledger(args: list[str], *, ledger_file: Path) -> str:
    result = subprocess.run(
        ["hledger", *args],
        env={**os.environ, "LEDGER_FILE": str(ledger_file)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise HledgerError(_sanitize_stderr(result.stderr, ledger_file))
    return result.stdout


def spending_report(period: str, category: str | None, *, ledger_file: Path) -> str:
    _validate_period(period)
    account = "expenses"
    if category is not None:
        _validate_category(category)
        account = category
    return run_hledger(["bal", account, "-p", period, "-2", "-S"], ledger_file=ledger_file)


def recent_transactions(limit: int, *, ledger_file: Path) -> str:
    return run_hledger(["reg", "expenses", "-n", str(limit)], ledger_file=ledger_file)


def search_transactions(query: str, *, ledger_file: Path) -> str:
    return run_hledger(["print", f"desc:{re.escape(query)}"], ledger_file=ledger_file)


def chunk_text(text: str, limit: int = 1900) -> list[str]:
    if not text:
        return []

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        elif not current:
            current = line
        else:
            current = f"{current}\n{line}"

        if len(current) > limit:
            chunks.append(current)
            current = ""

    if current:
        chunks.append(current)
    return chunks
