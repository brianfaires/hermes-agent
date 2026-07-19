"""Kanban notification routing policy helpers.

This module centralizes the policy for rows in ``kanban_notify_subs`` and
send-time delivery targets.  It deliberately does not mutate existing rows;
callers can use ``audit_notify_subs`` to report rows that no longer comply.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class NotifyTarget:
    platform: str
    chat_id: str
    thread_id: Optional[str] = None
    user_id: Optional[str] = None
    notifier_profile: Optional[str] = None

    def as_add_kwargs(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "notifier_profile": self.notifier_profile,
        }


def _load_cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def notification_policy(cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else _load_cfg()
    kanban = cfg.get("kanban") if isinstance(cfg, dict) else {}
    raw = (kanban or {}).get("notification_policy") or {}
    if isinstance(raw, str):
        raw = {"mode": raw}
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("mode") or "origin").strip().lower()
    if mode in {"telegram-only", "telegram_home", "telegram-home-only"}:
        mode = "telegram_home_only"
    allowed = raw.get("allowed_platforms")
    if allowed is None:
        allowed_platforms: list[str] = []
    elif isinstance(allowed, str):
        allowed_platforms = [p.strip().lower() for p in allowed.split(",") if p.strip()]
    else:
        allowed_platforms = [str(p).strip().lower() for p in allowed if str(p).strip()]
    return {
        "mode": mode,
        "allowed_platforms": allowed_platforms,
        "preserve_tui": bool(raw.get("preserve_tui", True)),
    }


def telegram_home_target(*, user_id: Optional[str] = None, notifier_profile: Optional[str] = None) -> Optional[NotifyTarget]:
    token = None
    reset_override = None
    try:
        from gateway.config import Platform, load_gateway_config
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.profiles import get_profile_dir

        reset_override = reset_hermes_home_override
        if notifier_profile:
            token = set_hermes_home_override(get_profile_dir(notifier_profile))
        gw_cfg = load_gateway_config()
        pcfg = gw_cfg.platforms.get(Platform.TELEGRAM)
        home = pcfg.home_channel if pcfg else None
    except Exception:
        home = None
    finally:
        if token is not None and reset_override is not None:
            reset_override(token)
    if not home or not getattr(home, "chat_id", None):
        return None
    return NotifyTarget(
        platform="telegram",
        chat_id=str(home.chat_id),
        thread_id=(str(home.thread_id) if getattr(home, "thread_id", None) else None),
        user_id=user_id,
        notifier_profile=notifier_profile,
    )


def resolve_notify_target(
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    cfg: Optional[dict[str, Any]] = None,
) -> Optional[NotifyTarget]:
    """Return the policy-approved target for a requested subscription.

    ``None`` means the subscription should not be created/delivered. Existing
    rows are never changed by this helper.
    """
    requested = NotifyTarget(
        platform=str(platform or "").lower(),
        chat_id=str(chat_id or ""),
        thread_id=(str(thread_id) if thread_id else None),
        user_id=user_id,
        notifier_profile=notifier_profile,
    )
    if not requested.platform or not requested.chat_id:
        return None

    policy = notification_policy(cfg)
    mode = policy["mode"]
    if mode in {"origin", "", "default"}:
        return requested

    allowed = set(policy.get("allowed_platforms") or [])
    if requested.platform in allowed:
        return requested
    if requested.platform == "tui" and policy.get("preserve_tui", True):
        return requested

    if mode == "telegram_home_only":
        return telegram_home_target(user_id=user_id, notifier_profile=notifier_profile)

    return requested


def is_notify_target_allowed(platform: str, *, cfg: Optional[dict[str, Any]] = None) -> bool:
    policy = notification_policy(cfg)
    mode = policy["mode"]
    p = str(platform or "").lower()
    if mode in {"origin", "", "default"}:
        return True
    if p in set(policy.get("allowed_platforms") or []):
        return True
    if p == "tui" and policy.get("preserve_tui", True):
        return True
    if mode == "telegram_home_only":
        return p == "telegram"
    return True


def audit_notify_subs(conn: Any, *, cfg: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """List existing subscription rows that violate the current policy."""
    from hermes_cli import kanban_db

    rows = kanban_db.list_notify_subs(conn)
    out: list[dict[str, Any]] = []
    for row in rows:
        target = resolve_notify_target(
            platform=row.get("platform") or "",
            chat_id=row.get("chat_id") or "",
            thread_id=row.get("thread_id") or None,
            user_id=row.get("user_id") or None,
            notifier_profile=row.get("notifier_profile") or None,
            cfg=cfg,
        )
        desired = None if target is None else {
            "platform": target.platform,
            "chat_id": target.chat_id,
            "thread_id": target.thread_id or "",
        }
        current = {
            "platform": str(row.get("platform") or ""),
            "chat_id": str(row.get("chat_id") or ""),
            "thread_id": str(row.get("thread_id") or ""),
        }
        if desired != current:
            out.append({**dict(row), "policy_target": desired})
    return out
