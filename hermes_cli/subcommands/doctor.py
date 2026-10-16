"""``hermes doctor`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable


def _db_timeout(value):
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("DB timeout must be an integer from 2 through 86400")
    if not 2 <= seconds <= 86400:
        raise argparse.ArgumentTypeError("DB timeout must be an integer from 2 through 86400")
    return seconds


def validate_doctor_args(args):
    full = getattr(args, "db_integrity", None) == "full"
    strict = getattr(args, "db_strict", False)
    timeout = getattr(args, "db_timeout", None)
    error = None
    if (full or strict) and (getattr(args, "fix", False) or getattr(args, "ack", None)):
        error = "--db-integrity full/--db-strict cannot be combined with --fix or --ack"
    elif full and getattr(args, "live", False):
        error = "--db-integrity full is database-only; cannot combine with --live"
    elif timeout is not None and not full:
        error = "--db-timeout requires --db-integrity full"
    elif timeout is not None and (type(timeout) is not int or not 2 <= timeout <= 86400):
        error = "DB timeout must be an integer from 2 through 86400"
    if error:
        print(f"hermes doctor: error: {error}", file=sys.stderr)
        raise SystemExit(2)


def build_doctor_parser(subparsers, *, cmd_doctor: Callable) -> None:
    """Attach the ``doctor`` subcommand to ``subparsers``."""
    # =========================================================================
    # doctor command
    # =========================================================================
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check configuration and dependencies",
        description="Diagnose issues with Hermes Agent setup",
    )
    doctor_parser.add_argument(
        "--fix", action="store_true",
        help="Attempt automatic fixes (outside the bounded/read-only DB guarantee)"
    )
    doctor_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Opt-in: run one bounded, read-only real-call health probe per "
            "configured tool backend (Firecrawl/FAL/browser/MCP/TTS/STT) "
            "after the static checks. Makes real network calls."
        ),
    )
    doctor_parser.add_argument(
        "--ack",
        metavar="ADVISORY_ID",
        default=None,
        help=(
            "Acknowledge a security advisory by ID and exit. After ack, the "
            "advisory will no longer trigger startup banners. Run `hermes "
            "doctor` first to see active advisories and their IDs."
        ),
    )
    doctor_parser.add_argument(
        "--db-integrity", choices=("full",), default=None,
        help="Database-only read-only full SQLite structural check; no FTS writes/foreign-key/application checks",
    )
    doctor_parser.add_argument(
        "--db-timeout", type=_db_timeout, default=None, metavar="SECONDS",
        help="Full DB mode only: finite total budget 2..86400 seconds (default 600)",
    )
    doctor_parser.add_argument(
        "--db-strict", action="store_true",
        help="Database-specific exit codes, not all-doctor strictness: 1 corruption, 3 incomplete, 4 error",
    )
    doctor_parser.set_defaults(func=cmd_doctor)
