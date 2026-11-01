"""Run-local permission to finish after removing one's own cron record.

Scripts run in another process. A private, short-lived receipt transports the same acknowledgement
as the in-process marker, without treating an operator's deletion as successful ownership.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory


_SCRIPT_RECEIPT_ENV = "_HERMES_CRON_SELF_REMOVAL"  # Internal subprocess protocol, not configuration.
logger = logging.getLogger(__name__)


@dataclass
class _SelfRemoval:
    job_id: str
    store: str
    receipt: Path
    owner: str | None
    removed: bool = False


_active: ContextVar[_SelfRemoval | None] = ContextVar("cron_self_removal", default=None)


@contextmanager
def self_removal_delivery_scope(job_id: str, *, expected_fire_owner: str | None = None):
    from cron.jobs import _current_cron_store

    with TemporaryDirectory(prefix="hermes-cron-self-removal-") as directory:
        marker = _SelfRemoval(job_id, str(_current_cron_store().cron_dir.resolve()),
                              Path(directory) / "removed", expected_fire_owner)
        token = _active.set(marker)
        try:
            yield marker
        finally:
            _active.reset(token)


def script_self_removal_env() -> dict[str, str]:
    marker = _active.get()
    if marker is None:
        return {}
    return {_SCRIPT_RECEIPT_ENV: json.dumps({
        "job_id": marker.job_id, "store": marker.store,
        "receipt": str(marker.receipt), "owner": marker.owner,
    })}


def record_self_removal(job: dict) -> None:
    """Called while holding the store lock, after this exact record was removed."""
    from cron.jobs import _current_cron_store

    store = str(_current_cron_store().cron_dir.resolve())
    marker = _active.get()
    owner = (job.get("fire_claim") or {}).get("by")
    if (marker is not None and marker.job_id == job["id"] and marker.store == store
            and (marker.owner is None or marker.owner == owner)):
        marker.removed = True
    raw = os.environ.get(_SCRIPT_RECEIPT_ENV)
    if not raw:
        return
    try:
        receipt = json.loads(raw)
        if (receipt["job_id"] == job["id"] and receipt["store"] == store
                and receipt["owner"] == owner):
            Path(receipt["receipt"]).write_text("removed", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        # Removal has committed. An unavailable acknowledgement must fail closed at the owner.
        logger.warning("Could not acknowledge cron script self-removal", exc_info=True)


def self_removal_delivery_allowed(job_id: str) -> bool:
    from cron.jobs import _current_cron_store, load_jobs

    marker = _active.get()
    if (marker is None or marker.job_id != job_id
            or marker.store != str(_current_cron_store().cron_dir.resolve())):
        return False
    if not marker.removed and not marker.receipt.is_file():
        return False
    # A replacement record with the same id belongs to another owner.
    return all(item.get("id") != job_id for item in load_jobs())
