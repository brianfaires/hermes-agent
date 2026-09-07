"""cron_calendar_mirror — cron schedule + run results on a Google Calendar.

Registration is deliberately tiny: one slash command, no hooks, no tools, no
background thread. The plugin never participates in scheduling or firing, so an
unconfigured, broken, or credential-less install cannot affect cron at all.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_HELP_TEXT = """\
/cron-calendar-mirror — mirror cron schedule + run results onto a calendar

Subcommands:
  status      Show configuration and credential readiness (no API calls)
  dry-run     Report the writes a sync would make, without making them
  sync        Reconcile the calendar with the current cron state

Configuration lives at plugins.entries.cron_calendar_mirror.settings:
  calendar_id             Hermes-owned secondary calendar id (required)
  horizon_days            How far ahead to project occurrences (default 7)
  approved_owner          Explicitly approved sole Calendar ACL owner (required)
  max_occurrences_per_job Cap on projected events per job (default 32)
  high_frequency_minutes  Below this cadence, use one all-day digest (default 60)
  result_page_size        Ledger page size; all results scanned (default 200)
  include_error_detail    Attach redacted failure text (default true)

This plugin never creates, fires, pauses, or deletes a cron job.
"""


def _handle_slash(raw_args: str) -> Optional[str]:
    argv = (raw_args or "").strip().split()
    sub = argv[0] if argv else "status"

    from plugins.cron_calendar_mirror import mirror

    if sub in {"help", "-h", "--help"}:
        return _HELP_TEXT
    if sub == "status":
        return mirror.status_text()
    if sub not in {"sync", "dry-run"}:
        return f"Unknown subcommand: {sub}\n\n{_HELP_TEXT}"

    from plugins.cron_calendar_mirror.calendar_client import CalendarError

    try:
        report = mirror.run_sync(dry_run=(sub == "dry-run"))
    except (mirror.ConfigurationError, CalendarError) as exc:
        return f"cron_calendar_mirror: not ready — {exc}"
    except Exception as exc:  # noqa: BLE001 - a slash command must not traceback
        logger.warning("cron_calendar_mirror sync failed (%s)", type(exc).__name__)
        return f"cron_calendar_mirror: sync failed ({type(exc).__name__})"
    return report.render()


def register(ctx) -> None:
    ctx.register_command(
        "cron-calendar-mirror",
        handler=_handle_slash,
        description="Mirror cron schedule and run results onto a Google Calendar.",
    )
