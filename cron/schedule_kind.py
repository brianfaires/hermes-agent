"""Recurrence shared by siblings first loaded after a source update.

Released processes may have cached timing-only ``cron.constants`` before the
checkout changed. Keep this callable in its own leaf so new siblings can load it.
"""


def is_recurring(job: dict) -> bool:
    """A cron/interval job (vs a one-shot); a null ``schedule`` counts as not recurring."""
    return (job.get("schedule") or {}).get("kind") in {"cron", "interval"}
