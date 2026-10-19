"""Identity-first ingress and session-free private command transport."""
from __future__ import annotations

from typing import Any, Optional
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


class BasePrivateIngressMixin:
    def _drop_unresolved(self, event: MessageEvent) -> bool:
        """True when *event* must be dropped: its identity could not be resolved because the route
        targets an unserved profile. Same disposition as the runner's ingress gate — one WARNING,
        never a fall-through to ``agent:main``."""
        from gateway.platforms.base import logger
        source = getattr(event, "source", None)
        if self._canonicalize(source) is not None:
            return False
        if getattr(source, "profile_route_rejected", False) is not True:
            return False
        logger.warning(
            "[%s] Dropping inbound event for %s: explicit profile route targets an unserved profile",
            self.name, getattr(source, "chat_id", "?"))
        return True

    def _event_session_key(self, event: MessageEvent) -> str:
        """Adapter-level session key for ``event``, profile-namespaced like the agent run."""
        return self._source_session_key(event.source)

    def _source_session_key(self, source: SessionSource) -> str:
        from gateway.platforms.base import build_session_key
        self._canonicalize(source)  # identity FIRST; no key derivation before it
        extra = self.config.extra
        return build_session_key(
            source, group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
            profile=self._session_key_profile(source))

    async def _dispatch_private_event(self, event):
        from gateway.private_commands import private_home_for_source, pin_private_command
        from hermes_cli.private_commands import match_private_command
        if not event.get_command():
            return False
        home = private_home_for_source(event.source)
        match = match_private_command(event._command_text(), home=home)
        if match is None:
            return False
        pin_private_command(event, home, match)
        try:
            response = await self._message_handler(event)
            if response:
                from gateway.platforms.base import _reply_anchor_for_event, _thread_metadata_for_source
                anchor = _reply_anchor_for_event(event)
                await self._send_with_retry(chat_id=event.source.chat_id, content=str(response),
                    reply_to=anchor, metadata=_thread_metadata_for_source(event.source, anchor))
        except Exception:  # health: allow BLE001 -- Transport errors may include private text; suppress exception details.
            from gateway.platforms.base import logger
            logger.warning('Private command delivery failed')
        return True
