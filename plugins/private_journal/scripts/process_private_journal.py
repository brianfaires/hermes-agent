#!/usr/bin/env python3
"""Cron wrapper template for the private-journal processor.

Copy this file to the default profile's $HERMES_HOME/scripts/ directory and
schedule it with:

    0 0 * * * process_private_journal.py

It intentionally reads the vault path from config.yaml:
plugins.entries.private-journal.vault_path
"""

from plugins.private_journal.processor import main


if __name__ == "__main__":
    raise SystemExit(main())
