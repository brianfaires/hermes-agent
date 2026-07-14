"""cron-calendar-sync — mirror cron jobs onto a Google Calendar.

User plugin (lives in ~/.hermes/plugins/, out of the hermes-agent repo). It
subscribes to the repo's cron lifecycle hooks (``cron.hooks``: CREATE / UPDATE
/ REMOVE / COMPLETE) so cron jobs are mirrored as calendar events, with event
length learned empirically from successful runs.

The hosting runtime auto-discovers this plugin and calls ``register(ctx)`` at
startup, which wires the calendar-sync handlers onto the cron hook registry.
All calendar work is best-effort: failures are logged, never raised, so cron
mutations and runs are unaffected, and the plugin self-disables when the
google-workspace skill / token are absent.
"""

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin entrypoint: register the cron-calendar-sync lifecycle handlers.

    Best-effort — a failure here must never break plugin loading or cron.
    """
    try:
        from . import calendar_sync
        calendar_sync.register()
        logger.info("cron-calendar-sync: registered cron lifecycle hooks")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("cron-calendar-sync: could not register hooks: %s", e)
