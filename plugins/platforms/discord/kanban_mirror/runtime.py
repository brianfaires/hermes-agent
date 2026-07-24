"""Discord–Kanban mirror runtime owned by the bundled Discord plugin.

The host ``GatewayRunner`` inherits this mixin so mirror-specific lifecycle,
identity routing, recovery, health, and durable response delivery stay beside
the Discord transport that provides their executable event boundary.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gateway.errors import MultiplexConfigError
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, Platform
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class DiscordKanbanMirrorRuntimeMixin:
    """Gateway-facing orchestration for the Discord–Kanban mirror."""


    def _init_discord_kanban_mirror_runtime(self) -> None:
        from plugins.platforms.discord.kanban_mirror.supervision import LoopSupervisor

        self._kanban_mirror_supervisor = LoopSupervisor()
        self._kanban_router_board_slug = None
        self._kanban_runtime_owner = None

    def _get_kanban_runtime_owner(self):
        if self._kanban_runtime_owner is None:
            from gateway.run import _resolve_kanban_runtime_owner

            self._kanban_runtime_owner = _resolve_kanban_runtime_owner(self)
        return self._kanban_runtime_owner

    def _start_discord_kanban_mirror_runtime(self) -> None:
        from plugins.platforms.discord.kanban_mirror.daemon import run_mirror_daemon

        owner = self._get_kanban_runtime_owner()
        if owner is not None and owner.mirror_config.enabled:
            async def run_owned_mirror() -> None:
                from gateway.run import _profile_runtime_scope

                with _profile_runtime_scope(owner.home):
                    await run_mirror_daemon(
                        lambda: self._running,
                        allow_process_token_fallback=not bool(
                            getattr(self.config, "multiplex_profiles", False)
                        ),
                    )

            self._kanban_mirror_supervisor.start(
                "reconciliation-lifecycle",
                run_owned_mirror,
            )
        self._start_kanban_router_runtime()

    async def _stop_discord_kanban_mirror_runtime(self) -> None:
        supervisor = getattr(self, "_kanban_mirror_supervisor", None)
        if supervisor is not None:
            await supervisor.stop()
        try:
            from gateway.status import write_runtime_status

            write_runtime_status(kanban_mirror={})
        except Exception:
            pass

    def _discord_adapter_for_profile(self, profile: str) -> Optional[BasePlatformAdapter]:
        """Resolve exactly one Discord bot identity, with no source fallback."""
        profile = str(profile or "").strip()
        if not profile:
            return None
        candidate = (
            self.adapters.get(Platform.DISCORD)
            if profile == self._gateway_profile_name
            else self._profile_adapters.get(profile, {}).get(Platform.DISCORD)
        )
        return candidate if candidate is not None and getattr(candidate, "_running", False) else None

    def _kanban_profile_adapters(self) -> dict[str, BasePlatformAdapter]:
        """Snapshot connected Discord identities by exact profile (never fallback)."""
        profiles: dict[str, BasePlatformAdapter] = {}
        for profile in (self._gateway_profile_name, *tuple(getattr(self, "_profile_adapters", {}))):
            adapter = self._discord_adapter_for_profile(profile)
            if adapter is not None:
                profiles[profile] = adapter
        return profiles

    def _all_kanban_profile_adapters(self) -> dict[str, BasePlatformAdapter]:
        """Snapshot Discord identities including stopped/disconnected adapters."""
        profiles: dict[str, BasePlatformAdapter] = {}
        primary = self.adapters.get(Platform.DISCORD)
        if primary is not None:
            profiles[self._gateway_profile_name] = primary
        for profile, platform_adapters in getattr(self, "_profile_adapters", {}).items():
            adapter = platform_adapters.get(Platform.DISCORD)
            if adapter is not None:
                profiles[profile] = adapter
        return profiles

    def _validate_kanban_router_readiness(self) -> str | None:
        """Validate configured ownership against live Discord bot identities."""
        from plugins.platforms.discord.kanban_mirror.inbox import validate_router_config
        get_owner = getattr(self, "_get_kanban_runtime_owner", None)
        if get_owner is not None:
            owner = get_owner()
        else:
            # Preserve the mixin's independently-testable/single-profile seam:
            # lightweight hosts written before owner resolution was introduced
            # may bind this method without binding the full runtime initializer.
            from types import SimpleNamespace
            from plugins.platforms.discord.kanban_mirror.config import load_mirror_config
            from plugins.platforms.discord.kanban_mirror.inbox import load_config

            owner = SimpleNamespace(
                mirror_config=load_mirror_config(),
                inbox_config=load_config(),
            )
        cfg = owner.inbox_config if owner is not None else None
        # Readiness is identity-bearing and must never survive a failed
        # revalidation (for example, bots swapped during reconnect).
        all_adapters = self._all_kanban_profile_adapters()
        for profile, adapter in all_adapters.items():
            adapter._kanban_router_ingress_identity = None
            adapter._kanban_router_profile = profile
        adapters = self._kanban_profile_adapters()
        if cfg is None or not (cfg.enabled and cfg.conversation_router_enabled):
            return None
        try:
            ingress_profile = validate_router_config(
                cfg, multiplex_profiles=bool(getattr(self.config, "multiplex_profiles", False)),
                mirror_config=owner.mirror_config,
            )
        except ValueError as exc:
            raise MultiplexConfigError(str(exc)) from exc
        errors = []
        for bot_id, profile in cfg.profile_bot_user_ids:
            adapter = adapters.get(profile)
            if adapter is None:
                errors.append(f"Discord adapter for profile '{profile}' is missing or disconnected")
                continue
            actual = str(getattr(getattr(getattr(adapter, "_client", None), "user", None), "id", "") or "")
            if not actual:
                errors.append(f"Discord adapter for profile '{profile}' has no connected user identity")
            elif actual != bot_id:
                errors.append(f"Discord bot user ID does not match profile '{profile}'")
        if errors:
            raise MultiplexConfigError("Discord conversation router readiness failed: " + "; ".join(errors))
        ingress = adapters[ingress_profile]
        ingress_bot_id = next(
            bot_id for bot_id, profile in cfg.profile_bot_user_ids
            if profile == ingress_profile
        )
        ingress._kanban_router_ingress_identity = (ingress_profile, ingress_bot_id)
        ingress._kanban_inbox_config = cfg
        self._kanban_router_ingress_profile = ingress_profile
        ingress.start_kanban_ingress_workers()
        return ingress_profile

    async def _revalidate_kanban_router_readiness(self) -> str | None:
        """Serialize reconnect-time, gateway-wide Discord identity validation."""
        lock = getattr(self, "_kanban_readiness_lock", None)
        if lock is None:
            lock = self._kanban_readiness_lock = asyncio.Lock()
        async with lock:
            try:
                ingress = self._validate_kanban_router_readiness()
            except MultiplexConfigError as exc:
                # Partial reconnects and swapped identities stay fail closed. A
                # later on_ready retries after the remaining identity is present.
                logger.warning("Kanban router reconnect readiness pending: %s", exc)
                return None
            if ingress:
                self._start_kanban_router_runtime()
            return ingress

    def _start_kanban_router_runtime(self, *, interval: float = 5.0,
                                     health_interval: float = 30.0) -> None:
        """Idempotently attach router recovery and health to the ingress gateway."""
        get_owner = getattr(self, "_get_kanban_runtime_owner", None)
        if callable(get_owner):
            owner = get_owner()
        else:
            # Preserve the independently-testable legacy/single-profile seam.
            from types import SimpleNamespace
            from plugins.platforms.discord.kanban_mirror.config import load_mirror_config
            from plugins.platforms.discord.kanban_mirror.inbox import load_config

            owner = SimpleNamespace(
                mirror_config=load_mirror_config(),
                inbox_config=load_config(),
            )
        cfg = owner.inbox_config if owner is not None else None
        if cfg is None:
            return
        ingress_profile = getattr(
            self,
            "_kanban_router_ingress_profile",
            getattr(self, "_gateway_profile_name", ""),
        )
        ingress = self._discord_adapter_for_profile(ingress_profile) if ingress_profile else None
        board_slug = cfg.board_slug or "default"
        enabled = bool(cfg.enabled and cfg.conversation_router_enabled and ingress)
        if not enabled:
            try:
                from gateway.status import write_runtime_status
                write_runtime_status(kanban_mirror={})
            except Exception:
                pass
            return
        self._kanban_router_board_slug = board_slug

        async def recover() -> None:
            from plugins.platforms.discord.kanban_mirror.recovery import run_outbound_recovery
            from plugins.platforms.discord.kanban_mirror.state import connect_mirror, mirror_db_path
            conn = connect_mirror(mirror_db_path(board_slug))
            try:
                while self._running:
                    current = cfg
                    ingress_connected = self._discord_adapter_for_profile(ingress_profile) is not None
                    active = bool(
                        current.enabled and current.conversation_router_enabled
                        and (current.board_slug or "default") == board_slug and ingress_connected
                    )
                    if not active:
                        await asyncio.sleep(interval)
                        continue
                    adapters = self._kanban_profile_adapters()

                    async def send(target, payload):
                        return await target.send(
                            payload["thread_id"], payload["content"],
                            reply_to=payload.get("reply_to_message_id"),
                            metadata={"thread_id": payload["thread_id"], "suppress_embeds": True},
                        )

                    await run_outbound_recovery(
                        conn, worker_id=f"gateway-{id(self):x}", adapters=adapters,
                        send=send, transition_publishers={}, include_transitions=False,
                    )
                    await asyncio.sleep(interval)
            finally:
                conn.close()

        async def recover_logs() -> None:
            from hermes_cli import kanban_db as kb
            from plugins.platforms.discord.kanban_mirror.conversation_log import recover_log_deliveries
            from plugins.platforms.discord.kanban_mirror.state import connect_mirror, mirror_db_path
            mirror_conn = connect_mirror(mirror_db_path(board_slug))
            board_conn = kb.connect(board=board_slug)
            worker_id = f"gateway-log-{id(self):x}"
            try:
                while self._running:
                    current = cfg
                    active = bool(
                        current.enabled and current.conversation_router_enabled
                        and current.conversation_log_enabled
                        and (current.board_slug or "default") == board_slug
                    )
                    if active:
                        def write_comment(task_id, payload, marker):
                            comment_id, _created = kb.add_comment_once(
                                board_conn, task_id,
                                author="discord:conversation-log-recovery",
                                body=f"{payload}\n\n{marker}",
                                idempotency_marker=marker,
                            )
                            return comment_id
                        # Consume frozen chunks only; never replay the command.
                        recover_log_deliveries(
                            mirror_conn, worker_id=worker_id,
                            write_comment=write_comment,
                        )
                    await asyncio.sleep(interval)
            finally:
                board_conn.close()
                mirror_conn.close()

        async def publish_health() -> None:
            from plugins.platforms.discord.kanban_mirror.state import connect_mirror, mirror_db_path
            from plugins.platforms.discord.kanban_mirror.supervision import health_snapshot
            from gateway.status import write_runtime_status
            conn = connect_mirror(mirror_db_path(board_slug))
            try:
                while self._running:
                    current = cfg
                    ingress_connected = self._discord_adapter_for_profile(ingress_profile) is not None
                    active = bool(current.enabled and current.conversation_router_enabled and ingress_connected)
                    snapshot = health_snapshot(
                        conn, router_enabled=active, ingress_connected=ingress_connected,
                        adapters=self._kanban_profile_adapters(),
                        supervisor=self._kanban_mirror_supervisor,
                    )
                    write_runtime_status(kanban_mirror=snapshot)
                    await asyncio.sleep(health_interval)
            finally:
                write_runtime_status(kanban_mirror={})
                conn.close()

        self._kanban_mirror_supervisor.start("outbound-recovery", recover)
        if bool(getattr(cfg, "conversation_log_enabled", False)):
            self._kanban_mirror_supervisor.start("log-delivery-recovery", recover_logs)
        self._kanban_mirror_supervisor.start("health-publication", publish_health)

    @staticmethod
    def _is_mirrored_kanban_conversation_event(event: Any, source: Any) -> bool:
        """Recognize durable mirrored routes, not ordinary Discord traffic."""
        return bool(
            event
            and getattr(source, "platform", None) == Platform.DISCORD
            and getattr(event, "outbound_profile", None)
            and getattr(event, "correlation_id", None)
            and getattr(event, "route_marker", None) in {
                "discord-kanban-conversation", "discord-kanban-directive",
            }
            and str(getattr(source, "thread_id", "") or "").strip()
        )

    async def _deliver_mirrored_kanban_response(
        self, *, event: MessageEvent, source: SessionSource, content: str,
    ) -> bool:
        """Freeze, durably enqueue, confirm-send, and ledger one agent reply."""
        from plugins.platforms.discord.kanban_mirror.context import resolve_mirrored_kanban_thread
        from plugins.platforms.discord.kanban_mirror.conversation_log import record_conversation_event
        from plugins.platforms.discord.kanban_mirror.outbox import OutboundEnvelope, deliver, enqueue, fail_closed
        from plugins.platforms.discord.kanban_mirror.state import active_thread_binding, connect_mirror, mirror_db_path

        thread_id = str(getattr(source, "thread_id", None) or source.chat_id or "").strip()
        profile = str(getattr(event, "outbound_profile", "") or "").strip()
        resolved = resolve_mirrored_kanban_thread(thread_id)
        if resolved is None or resolved.initiative_kind == "ambiguous":
            logger.error("Mirrored response cannot resolve one durable mirror DB for thread %s", thread_id)
            return False
        conn = connect_mirror(mirror_db_path(resolved.board_slug))
        try:
            binding = active_thread_binding(conn, thread_id)
            binding_key = binding.binding_key if binding is not None else None
            envelope = OutboundEnvelope(
                profile=profile,
                thread_id=thread_id,
                reply_to_message_id=str(getattr(event, "message_id", "") or "") or None,
                content=content,
                attachments=(),
                correlation_id=str(getattr(event, "correlation_id", "") or ""),
                binding_key=binding_key,
            )
            operation_id = enqueue(conn, envelope)
            if getattr(event, "media_urls", None) or "MEDIA:" in content or len(content) > 1900:
                fail_closed(conn, operation_id, "unsupported mirrored media or oversized response")
                return False
            adapter = self._discord_adapter_for_profile(profile)

            async def _send(target, payload):
                return await target.send(
                    payload["thread_id"], payload["content"],
                    reply_to=payload.get("reply_to_message_id"),
                    metadata={"thread_id": payload["thread_id"], "suppress_embeds": True},
                )

            def _record_confirmed(discord_message_id: str, payload: dict[str, Any]) -> None:
                record_conversation_event(
                    conn,
                    discord_message_id=discord_message_id,
                    thread_id=payload["thread_id"],
                    binding_key=payload.get("binding_key"),
                    event_class="conversation.agent",
                    author_label=payload["profile"],
                    content=payload["content"],
                    replied_to_message_id=payload.get("reply_to_message_id"),
                    commit=False,
                )

            return await deliver(
                conn, operation_id, adapter, send=_send, on_confirmed=_record_confirmed,
            )
        finally:
            conn.close()
