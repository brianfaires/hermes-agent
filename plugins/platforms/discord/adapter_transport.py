"""Discord client construction and common cached/REST channel resolution."""
from __future__ import annotations

from typing import Any


class DiscordTransportMixin:
    _client: Any

    def _build_client(self, **options):
        """Construct a fresh Bot before login; subclasses may add SDK transport options."""
        from discord.ext import commands
        return commands.Bot(**options)

    async def _connect_voice_channel(self, channel):
        """Connect the native voice transport; subclasses may supply an SDK client."""
        return await channel.connect()

    async def _resolve_channel(self, channel_id: Any) -> Any:
        """Cached ``get_channel`` first, REST ``fetch_channel`` on miss (raises on API error).

        Every outbound target funnels through here (home channel, cron ``discord:<target>``,
        thread metadata), so a pasted channel link is accepted at this one boundary instead of
        dying in ``int()`` as a generic send failure."""
        from plugins.platforms.discord.adapter import discord_channel_id_from_link
        if isinstance(channel_id, str):
            channel_id = discord_channel_id_from_link(channel_id.strip()) or channel_id
        channel = self._client.get_channel(int(channel_id))
        if not channel:
            channel = await self._client.fetch_channel(int(channel_id))
        return channel
