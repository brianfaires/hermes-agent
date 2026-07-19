"""Config for the Kanban Discord mirror daemon.

Loaded from config.yaml under ``kanban.discord_mirror``. This module is the
ONLY place profile-specific values (which bot token, which board, which forum
channel) enter the system — everything else is profile-agnostic.

Example config.yaml:

    kanban:
      discord_mirror:
        enabled: true
        board: operations
        forum_channel_id: "123..."
        guild_id: "456..."
        token_env_path: ~/.hermes/profiles/ops/.env   # DISCORD_BOT_TOKEN read from here
    auxiliary:
      kanban_mirror:            # model for briefs/notes/curation (async_call_llm task name)
        provider: openrouter
        model: some/cheap-model
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hermes_constants import get_hermes_home

from gateway.kanban_mirror.closed_thread_policy import ClosedThreadPolicy, load_closed_thread_policy


def _number(value, default, converter):
    """Parse an optional numeric setting without breaking gateway startup."""
    try:
        return converter(value)
    except (TypeError, ValueError):
        return default


def _default_token_env_path() -> Path:
    # Resolved at load time so a named-profile gateway reads its own .env,
    # not the default profile's.
    return get_hermes_home() / ".env"


@dataclass(frozen=True)
class MirrorConfig:
    enabled: bool = False
    board: str = "default"
    forum_channel_id: str = ""
    guild_id: str = ""
    token_env_path: Path = field(default_factory=_default_token_env_path)
    poll_seconds: float = 10.0
    prose_interval_seconds: float = 60.0
    max_post_chars: int = 3800
    note_char_limit: int = 900
    digest_title: str = "Board"
    done_thread_archive_idle_minutes: float = 60.0
    closed_thread_reply_policy: ClosedThreadPolicy = field(default_factory=ClosedThreadPolicy)
    binding_transitions_enabled: bool = False
    terminal_lifecycle_enabled: bool = False
    reconciliation_enabled: bool = False
    automatic_successor_enabled: bool = False

    def valid(self) -> bool:
        return bool(self.enabled and self.board and self.forum_channel_id)


def load_mirror_config(raw_config: dict | None = None) -> MirrorConfig:
    if raw_config is None:
        try:
            from hermes_cli.config import read_raw_config
            raw_config = read_raw_config() or {}
        except Exception:
            raw_config = {}
    kanban_cfg = raw_config.get("kanban") if isinstance(raw_config, dict) else {}
    cfg = kanban_cfg.get("discord_mirror") if isinstance(kanban_cfg, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}
    result = MirrorConfig(
        enabled=bool(cfg.get("enabled", False)),
        board=str(cfg.get("board") or "default").strip(),
        forum_channel_id=str(cfg.get("forum_channel_id") or "").strip(),
        guild_id=str(cfg.get("guild_id") or "").strip(),
        token_env_path=Path(str(cfg.get("token_env_path") or _default_token_env_path())).expanduser(),
        poll_seconds=_number(cfg.get("poll_seconds", 10.0), 10.0, float),
        prose_interval_seconds=_number(cfg.get("prose_interval_seconds", 60.0), 60.0, float),
        max_post_chars=_number(cfg.get("max_post_chars", 3800), 3800, int),
        note_char_limit=_number(cfg.get("note_char_limit", 900), 900, int),
        digest_title=str(cfg.get("digest_title") or "Board"),
        done_thread_archive_idle_minutes=_number(
            cfg.get("done_thread_archive_idle_minutes", 60.0), 60.0, float
        ),
        closed_thread_reply_policy=load_closed_thread_policy(cfg.get("closed_thread_reply_policy")),
        binding_transitions_enabled=bool(cfg.get("binding_transitions_enabled", False)),
        terminal_lifecycle_enabled=bool(cfg.get("terminal_lifecycle_enabled", False)),
        reconciliation_enabled=bool(cfg.get("reconciliation_enabled", False)),
        automatic_successor_enabled=bool(cfg.get("automatic_successor_enabled", False)),
    )
    if (result.reconciliation_enabled or result.terminal_lifecycle_enabled or result.automatic_successor_enabled) and not result.binding_transitions_enabled:
        raise ValueError(
            "kanban.discord_mirror binding_transitions_enabled is required when "
            "reconciliation_enabled, terminal_lifecycle_enabled, or automatic_successor_enabled is enabled"
        )
    return result
