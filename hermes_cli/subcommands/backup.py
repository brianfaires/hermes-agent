"""``hermes backup`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_backup_parser(subparsers, *, cmd_backup: Callable) -> None:
    """Attach the ``backup`` subcommand to ``subparsers``."""
    backup_parser = subparsers.add_parser(
        "backup",
        help="Back up Hermes home directory to a zip file",
        description=(
            "Create a zip archive of your entire Hermes configuration, "
            "skills, sessions, and data (excludes the hermes-agent codebase). "
            "Use --quick for a fast snapshot of just critical state files. "
            "Pass -o - to stream a full archive to stdout (status on stderr)."
        ),
    )
    backup_parser.add_argument(
        "-o",
        "--output",
        help=(
            "Output path for the zip file (default: ~/hermes-backup-<timestamp>.zip). "
            "Use '-' to stream ZIP bytes to stdout while status goes to stderr."
        ),
    )
    backup_parser.add_argument(
        "--staging-dir",
        help=(
            "Directory for temporary SQLite snapshots when streaming with -o - "
            "(default: HERMES_HOME/backups). File output always stages beside the zip."
        ),
    )
    backup_parser.add_argument(
        "-q",
        "--quick",
        action="store_true",
        help="Quick snapshot: only critical state files (config, state.db, .env, auth, cron)",
    )
    backup_parser.add_argument(
        "-l", "--label", help="Label for the snapshot (only used with --quick)"
    )
    backup_parser.set_defaults(func=cmd_backup)
