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
        from hermes_cli.config import get_config_path, load_config
        from yaml import safe_load
        # Diagnose malformed files before the compatibility loader can fall
        # back to defaults; behavioral values still use its managed overlay.
        path = get_config_path()
        if path.exists():
            raw = safe_load(path.read_text(encoding="utf-8"))
            if raw is not None and not isinstance(raw, dict):
                raise ValueError("notification configuration root must be a mapping")
        return load_config() or {}
    except Exception:
        return {"kanban": {"notification_policy": "deny"}}


def notification_policy(cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else _load_cfg()
    if not isinstance(cfg, dict):
        return {"mode": "deny", "allowed_platforms": [], "preserve_tui": True}
    kanban = cfg.get("kanban")
    if not isinstance(kanban, dict) and kanban is not None:
        return {"mode": "deny", "allowed_platforms": [], "preserve_tui": True}
    raw = (kanban or {}).get("notification_policy")
    if raw is None:
        raw = {"mode": "deny"} if "notification_policy" in (kanban or {}) else {}
    if isinstance(raw, str):
        raw = {"mode": raw or "deny"}
    if not isinstance(raw, dict):
        # A present but malformed policy must not silently restore the
        # permissive origin default.
        raw = {"mode": "deny"}
    mode = str(raw.get("mode", "origin")).strip().lower()
    if mode in {"telegram-only", "telegram_home", "telegram-home-only"}:
        mode = "telegram_home_only"
    elif mode not in {"origin", "default", "telegram_home_only"}:
        # A typo in a restrictive policy must not silently restore origin
        # delivery. Explicitly allowed platforms and TUI preservation still
        # apply below; every other target fails closed.
        mode = "deny"
    allowed = raw.get("allowed_platforms")
    if allowed is None:
        allowed_platforms: list[str] = []
    elif isinstance(allowed, str):
        allowed_platforms = [p.strip().lower() for p in allowed.split(",") if p.strip()]
    elif isinstance(allowed, (list, tuple, set, frozenset)):
        allowed_platforms = [p.strip().lower() for p in allowed if isinstance(p, str) and p.strip()]
    else:
        # Invalid allow-list values grant nothing rather than raising during
        # notification delivery or broadening policy.
        allowed_platforms = []
    return {
        "mode": mode,
        "allowed_platforms": allowed_platforms,
        "preserve_tui": raw.get("preserve_tui", True) is True,
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
            from hermes_cli.profiles import get_active_profile_name
            if notifier_profile != (get_active_profile_name() or "default"):
                home_path = get_profile_dir(notifier_profile)
                if not home_path.is_dir():
                    return None
                token = set_hermes_home_override(home_path)
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


def _resolve_notify_target(
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

    return None


def resolve_notify_target(*, profile_home=None, **kwargs) -> Optional[NotifyTarget]:
    """Resolve under the subscription owner's private configuration scope."""
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.profiles import get_active_profile_name, get_profile_dir
    from agent.secret_scope import refresh_profile_secret_scope, set_secret_scope, reset_secret_scope
    from pathlib import Path

    owner = kwargs.get("notifier_profile")
    home = Path(profile_home) if profile_home is not None else get_hermes_home()
    try:
        if profile_home is None and owner and owner != (get_active_profile_name() or "default"):
            home = get_profile_dir(owner)
            if not home.is_dir():
                return None
        foreign_home = home.resolve() != get_hermes_home().resolve()
        home_token = set_hermes_home_override(home)
        try:
            secrets = refresh_profile_secret_scope(home, inherit_process_secrets=not foreign_home)
            if foreign_home:
                # A single-profile caller may explicitly subscribe for another
                # owner. Mask ambient misses privately, without changing the
                # process-wide multiplex flag or the single-profile overlay.
                import os
                secrets = {**dict.fromkeys(os.environ, ""), **secrets}
            secret_token = set_secret_scope(secrets)
            try:
                # Already selected the owner's home; avoid a second name lookup
                # for custom mounted profiles when loading the Telegram home.
                target = _resolve_notify_target(**{**kwargs, "notifier_profile": None})
                if target is None:
                    return None
                return NotifyTarget(target.platform, target.chat_id, target.thread_id, target.user_id, owner)
            finally:
                reset_secret_scope(secret_token)
        finally:
            reset_hermes_home_override(home_token)
    except Exception:
        return None


def policy_subscription(row: dict, *, cfg=None, profile_home=None) -> Optional[dict]:
    """Return a delivery copy; retain the original row for cursor operations."""
    target = resolve_notify_target(
        platform=row.get("platform"), chat_id=row.get("chat_id"),
        thread_id=row.get("thread_id"), user_id=row.get("user_id"),
        notifier_profile=row.get("notifier_profile"), cfg=cfg, profile_home=profile_home,
    )
    if target is None:
        return None
    resolved = {**row, **target.as_add_kwargs()}
    if any(str(row.get(k) or "") != str(resolved.get(k) or "") for k in ("platform", "chat_id", "thread_id")):
        # Origin reply IDs, alternate identities and active-wake instructions
        # belong to the origin session, never to a policy redirect destination.
        resolved.update(delivery_metadata=None, delivery_mode="notify", chat_type="channel", user_id=None, user_id_alt=None)
    return resolved


def subscribe_notify(conn, **kwargs) -> Optional[dict]:
    from hermes_cli import kanban_db
    resolved = policy_subscription(kwargs)
    if resolved is not None:
        kanban_db.add_notify_sub(conn, **resolved)
    return resolved


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
    return False


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
