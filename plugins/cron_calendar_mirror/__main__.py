"""``python -m plugins.cron_calendar_mirror [sync|status] [--dry-run]``."""

from plugins.cron_calendar_mirror.mirror import main

raise SystemExit(main())
