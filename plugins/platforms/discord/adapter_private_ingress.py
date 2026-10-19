"""Shared channel policy and direct commands before Discord conversational dispatch."""
from __future__ import annotations

import re
from typing import Any, Optional


class DiscordPrivateIngressMixin:
    def _thread_id_and_chat_for_channel(self, channel) -> tuple[Optional[str], Optional[str]]:
        """Return ``(thread_id, chat_id)``; for a thread chat_id is the thread id (dispatch session key)."""
        from plugins.platforms.discord.adapter import discord
        if channel is None:
            return None, None
        chan_id = getattr(channel, "id", None)
        if chan_id is None:
            return None, None
        is_thread = isinstance(channel, getattr(discord, "Thread", ()))
        return (str(chan_id) if is_thread else None), str(chan_id)


    def _discord_channel_policy(self, message, channel_keys, *, mention_prefix=False):
        """Apply the same configured channel and trigger gates before any dispatch."""
        allowed = self._get_allowed_channels()
        ignored = self._get_ignored_channels()
        if ((allowed and "*" not in allowed and not (channel_keys & allowed))
                or "*" in ignored or channel_keys & ignored):
            return False, False, False
        free_channels = self._discord_free_response_channels()
        voice_linked = str(message.channel.id) in {str(value) for value in self._voice_text_channels.values()}
        free = "*" in free_channels or bool(channel_keys & free_channels) or voice_linked
        triggered = (not self._discord_require_mention() or free or self._in_bot_thread(message)
                     or mention_prefix or self._self_is_explicitly_mentioned(message)
                     or self._is_bot_tag_debounce_continuation(message))
        return triggered, free, voice_linked

    def _private_discord_channel_allowed(self, message):
        from plugins.platforms.discord.adapter import discord
        if isinstance(message.channel, discord.DMChannel) or message.guild is None:
            return True
        parent_id = self._get_parent_channel_id(message.channel)
        keys = self._discord_channel_keys(message, parent_id)
        return self._discord_channel_policy(message, keys)[0]

    def _private_discord_event(self, message, role_authorized=False):
        from gateway.platforms.event import MessageEvent, MessageType
        text = message.content or ''
        bot_id = getattr(getattr(self._client, 'user', None), 'id', None)
        if bot_id:
            text = re.sub(r'^\s*<@!?' + re.escape(str(bot_id)) + r'>\s*', '', text, count=1)
        if not text.lstrip().startswith('/'):
            return None
        channel = message.channel
        thread_id, chat_id = self._thread_id_and_chat_for_channel(channel)
        guild = getattr(message, 'guild', None)
        source = self.build_source(chat_id=chat_id, user_id=str(message.author.id),
            user_name=getattr(message.author, 'display_name', None),
            chat_type='dm' if guild is None else ('thread' if thread_id else 'group'),
            thread_id=thread_id, guild_id=str(guild.id) if guild else None,
            parent_chat_id=str(getattr(channel, 'parent_id', '') or '') or None,
            role_authorized=role_authorized, message_id=str(message.id))
        attached = bool(getattr(message, 'attachments', []))
        return MessageEvent(text=text, message_type=MessageType.DOCUMENT if attached else MessageType.COMMAND,
                            source=source, raw_message=message, message_id=str(message.id))

    def _is_private_discord_message(self, message):
        from gateway.private_commands import private_home_for_source
        from hermes_cli.private_commands import match_private_command
        event = self._private_discord_event(message)
        return event is not None and match_private_command(
            event.text, home=private_home_for_source(event.source)) is not None

    async def _handle_message(self, message, role_authorized=False, *, recovered=False):
        if self._is_private_discord_message(message):
            # Recovery must not duplicate private captures after a restart.
            if recovered:
                return False
            # Live ingress already authorizes the user and claims dedup. Rechecking
            # that claim here would reject every message that reached this handler.
            if not self._private_discord_channel_allowed(message):
                return False
            event = self._private_discord_event(message, role_authorized)
            await self._dispatch_private_event(event)
            return True
        return await self._handle_conversational_message(message, role_authorized, recovered=recovered)
