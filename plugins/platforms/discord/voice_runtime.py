"""Discord voice routing and adapter orchestration owned by the platform edge.

The mixin uses the gateway runner only for platform-neutral services such as
session resolution, authorization, persisted voice-mode state, and TTS.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import sys
import time
from types import SimpleNamespace
from typing import Optional

from agent.async_utils import safe_schedule_threadsafe
from agent.i18n import t
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


class DiscordVoiceRuntimeMixin:
    """Discord-specific capture, guild routing, and adapter orchestration."""

    @staticmethod
    def _get_guild_id(event: MessageEvent) -> Optional[int]:
        """Extract Discord guild_id from the raw message object."""
        raw = getattr(event, "raw_message", None)
        if raw is None:
            return None
        if hasattr(raw, "guild_id") and raw.guild_id:
            return int(raw.guild_id)
        if hasattr(raw, "guild") and raw.guild:
            return raw.guild.id
        return None

    def _voice_channel_sidecar_note(
        self, event, source: SessionSource, session_key: str
    ) -> Optional[str]:
        """Return a Discord voice-channel note only when channel state changes."""
        if source.platform != Platform.DISCORD:
            return None
        adapter = self.adapters.get(Platform.DISCORD)
        guild_id = self._get_guild_id(event)
        if not (
            guild_id
            and adapter
            and hasattr(adapter, "get_voice_channel_context")
        ):
            return None
        try:
            voice_context = adapter.get_voice_channel_context(guild_id) or ""
        except Exception:
            logger.debug("voice-channel context read failed", exc_info=True)
            return None
        if not hasattr(self, "_session_vc_last"):
            self._session_vc_last = {}
        previous = self._session_vc_last.get(session_key) if session_key else None
        if session_key:
            self._session_vc_last[session_key] = voice_context
        if voice_context == (previous if previous is not None else ""):
            return None
        if not voice_context:
            return "[Voice channel now: not connected to a voice channel]"
        return f"[Voice channel now: {voice_context}]"

    def _discord_voice_ack_callback_for_turn(self, source, run_still_current):
        """Build the Discord-only first-tool verbal acknowledgement callback."""
        if source.platform != Platform.DISCORD:
            return None
        adapter = self._adapter_for_source(source)
        voice_text_channels = getattr(adapter, "_voice_text_channels", None)
        if not isinstance(voice_text_channels, dict) or not hasattr(
            adapter, "voice_mixer_active"
        ):
            return None

        guild_id = None
        for candidate_guild_id, text_channel_id in voice_text_channels.items():
            if (
                str(text_channel_id) == str(source.chat_id)
                and adapter.voice_mixer_active(candidate_guild_id)
            ):
                guild_id = candidate_guild_id
                break
        if guild_id is None:
            return None

        loop = asyncio.get_running_loop()
        fired = False

        def voice_ack_callback(_call_id, _tool_name, _args):
            nonlocal fired
            if fired or not run_still_current():
                return
            fired = True
            active_adapter = self._adapter_for_source(source)
            if active_adapter is None or not hasattr(
                active_adapter, "play_ack_in_voice"
            ):
                return
            try:
                safe_schedule_threadsafe(
                    active_adapter.play_ack_in_voice(
                        guild_id,
                        model_name=self._voice_ack_model_for_source(source),
                    ),
                    loop,
                    logger=logger,
                    log_message="voice ack scheduling error",
                )
            except Exception as error:
                logger.debug("voice ack schedule failed: %s", error)

        return voice_ack_callback

    @staticmethod
    def _configured_discord_voice_ack(adapter, key: str) -> Optional[str]:
        """Choose a configured Discord voice acknowledgement, if any."""
        config = getattr(adapter, "_voice_fx_cfg", None)
        if not isinstance(config, dict):
            return None
        raw = config.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            return None
        phrases = [str(item).strip() for item in raw if str(item).strip()]
        return random.choice(phrases) if phrases else None

    @staticmethod
    def _configured_discord_voice_int(adapter, key: str, default: int) -> int:
        config = getattr(adapter, "_voice_fx_cfg", None)
        raw = config.get(key, default) if isinstance(config, dict) else default
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return default

    def _voice_session_user_turn_count(self, source: SessionSource) -> int:
        """Return persisted user-turn count for a voice-linked session."""
        store = getattr(self, "session_store", None)
        if store is None:
            return 0
        try:
            entry = store.get_or_create_session(source)
            history = store.load_transcript(entry.session_id) or []
        except Exception:
            logger.debug(
                "Could not load voice-session history for join acknowledgement",
                exc_info=True,
            )
            return 0
        return sum(1 for message in history if message.get("role") == "user")

    def _voice_ack_model_for_source(self, source: SessionSource) -> str:
        """Resolve the active model used by the Discord acknowledgement catalog."""
        try:
            model, _runtime = self._resolve_session_agent_runtime(source=source)
            return str(model or "")
        except Exception:
            logger.debug("Could not resolve voice acknowledgement model", exc_info=True)
            return ""

    def _discord_voice_join_ack_text(
        self,
        source: SessionSource,
        *,
        adapter=None,
        rejoining_persisted_voice_session: bool = False,
    ) -> str:
        """Return the spoken Discord VC join acknowledgement for this session."""
        try:
            if self._session_key_for_source(source) in (self._running_agents or {}):
                return (
                    self._configured_discord_voice_ack(adapter, "busy_ack_phrases")
                    or "working, one sec"
                )
        except Exception:
            pass
        if rejoining_persisted_voice_session:
            return (
                self._configured_discord_voice_ack(adapter, "restart_join_ack_phrases")
                or "Back online."
            )
        threshold = self._configured_discord_voice_int(
            adapter, "session_resume_user_turn_threshold", 2
        )
        if self._voice_session_user_turn_count(source) > threshold:
            return (
                self._configured_discord_voice_ack(
                    adapter, "session_resume_ack_phrases"
                )
                or "Picking up where we left off."
            )
        return (
            self._configured_discord_voice_ack(adapter, "join_ack_phrases")
            or t("gateway.voice.connected_spoken")
        )

    async def _wait_for_discord_voice_ready(
        self, adapter, guild_id: int, *, timeout: float = 3.0
    ) -> bool:
        """Wait briefly for Discord voice to be connected before first playback."""
        is_in_voice_channel = getattr(adapter, "is_in_voice_channel", None)
        if not callable(is_in_voice_channel):
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                ready = is_in_voice_channel(guild_id)
                if inspect.isawaitable(ready):
                    ready = await ready
                if ready:
                    return True
            except Exception:
                logger.debug("Discord voice readiness check failed", exc_info=True)
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.1, remaining))

    @staticmethod
    def _discord_adapter_connected_to_voice_channel(
        adapter, guild_id: int, channel_id: int
    ) -> bool:
        voice_clients = getattr(adapter, "_voice_clients", {}) or {}
        existing = (
            voice_clients.get(int(guild_id))
            if isinstance(voice_clients, dict)
            else None
        )
        if existing is None:
            return False
        try:
            return bool(
                existing.is_connected()
                and getattr(getattr(existing, "channel", None), "id", None)
                == int(channel_id)
            )
        except Exception:
            return False

    def _wire_discord_voice_callbacks(self, adapter) -> None:
        """Bind receive/cleanup callbacks to the profile adapter owning the VC."""
        if hasattr(adapter, "_voice_input_callback"):

            async def _input(guild_id, user_id, transcript):
                await self._handle_voice_channel_input(
                    guild_id, user_id, transcript, adapter=adapter
                )

            adapter._voice_input_callback = _input
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = (
                lambda chat_id: self._handle_voice_timeout_cleanup(
                    chat_id, adapter=adapter
                )
            )
        if hasattr(adapter, "_voice_mode_getter"):
            adapter._voice_mode_getter = lambda chat_id: self._voice_mode.get(
                self._voice_key(Platform.DISCORD, str(chat_id)), "off"
            )

    def _clear_discord_voice_callbacks_if_idle(self, adapter) -> None:
        if getattr(adapter, "_voice_clients", {}) or {}:
            return
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = None

    async def _handle_discord_auto_voice_join(
        self, adapter, member, voice_channel
    ) -> bool:
        """Join configured Discord voice presence on the owning profile adapter."""
        guild = getattr(voice_channel, "guild", None) or getattr(
            member, "guild", None
        )
        guild_id = getattr(guild, "id", None)
        channel_id = getattr(voice_channel, "id", None)
        if guild_id is None or channel_id is None:
            return False
        already_connected = self._discord_adapter_connected_to_voice_channel(
            adapter, int(guild_id), int(channel_id)
        )
        self._wire_discord_voice_callbacks(adapter)
        try:
            if not await adapter.join_voice_channel(voice_channel):
                self._clear_discord_voice_callbacks_if_idle(adapter)
                return False
        except Exception:
            logger.warning(
                "Failed to auto-join Discord voice channel", exc_info=True
            )
            self._clear_discord_voice_callbacks_if_idle(adapter)
            return False

        chat_id = str(channel_id)
        rejoining_persisted_voice_session = self._voice_mode.get(
            self._voice_key(Platform.DISCORD, chat_id)
        ) in {"voice_only", "all"}
        adapter._voice_text_channels[int(guild_id)] = int(channel_id)
        getattr(adapter, "_auto_voice_session_channels", set()).add(chat_id)
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            chat_name=getattr(voice_channel, "name", None),
            chat_type="group",
            user_id=str(getattr(member, "id", "")) or None,
            user_name=getattr(member, "display_name", None),
            guild_id=str(guild_id),
            profile=getattr(adapter, "_runtime_profile_name", None),
        )
        if hasattr(adapter, "_voice_sources"):
            adapter._voice_sources[int(guild_id)] = source.to_dict()
        self._voice_mode[self._voice_key(Platform.DISCORD, chat_id)] = "voice_only"
        self._save_voice_modes()
        self._set_adapter_auto_tts_enabled(adapter, chat_id, enabled=True)
        if not already_connected:
            if not await self._wait_for_discord_voice_ready(adapter, int(guild_id)):
                logger.warning(
                    "Discord auto-voice greeting skipped: voice connection not "
                    "ready for guild %s",
                    guild_id,
                )
                return True
            event = MessageEvent(
                source=source,
                text="",
                message_type=MessageType.TEXT,
                raw_message=SimpleNamespace(guild_id=int(guild_id), guild=guild),
            )
            await self._send_voice_reply(
                event,
                self._discord_voice_join_ack_text(
                    source,
                    adapter=adapter,
                    rejoining_persisted_voice_session=(
                        rejoining_persisted_voice_session
                    ),
                ),
            )
        return True

    async def _handle_discord_auto_voice_leave(
        self, adapter, member, voice_channel
    ) -> bool:
        guild = getattr(voice_channel, "guild", None) or getattr(
            member, "guild", None
        )
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            return False
        chat_id = str(
            getattr(adapter, "_voice_text_channels", {}).get(int(guild_id))
            or getattr(voice_channel, "id", "")
        )
        try:
            await adapter.leave_voice_channel(int(guild_id))
        except Exception:
            logger.warning(
                "Failed to auto-leave Discord voice channel", exc_info=True
            )
        if chat_id:
            self._voice_mode[self._voice_key(Platform.DISCORD, chat_id)] = "off"
            self._save_voice_modes()
            self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
            getattr(adapter, "_auto_voice_session_channels", set()).discard(chat_id)
        self._clear_discord_voice_callbacks_if_idle(adapter)
        return True

    async def _handle_voice_channel_join(self, event: MessageEvent) -> str:
        """Join the user's current Discord voice channel."""
        adapter = self._adapter_for_source(event.source)
        if adapter is None or not hasattr(adapter, "join_voice_channel"):
            return "Voice channels are not supported on this platform."

        guild_id = self._get_guild_id(event)
        if not guild_id:
            return "This command only works in a Discord server."

        voice_channel = await adapter.get_user_voice_channel(
            guild_id, event.source.user_id
        )
        if not voice_channel:
            return "You need to be in a voice channel first."

        rejoining_persisted_voice_session = self._voice_mode.get(
            self._voice_key(event.source.platform, event.source.chat_id)
        ) in {"voice_only", "all"}
        self._wire_discord_voice_callbacks(adapter)

        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as exc:
            logger.warning("Failed to join voice channel: %s", exc)
            adapter._voice_input_callback = None
            err_lower = str(exc).lower()
            if any(name in err_lower for name in ("pynacl", "nacl", "davey")):
                return (
                    "Voice dependencies are missing (PyNaCl / davey). "
                    f"Install with: `{sys.executable} -m pip install PyNaCl`"
                )
            return f"Failed to join voice channel: {exc}"

        if success:
            adapter._voice_text_channels[guild_id] = int(event.source.chat_id)
            if hasattr(adapter, "_voice_sources"):
                adapter._voice_sources[guild_id] = event.source.to_dict()
            self._voice_mode[
                self._voice_key(event.source.platform, event.source.chat_id)
            ] = "voice_only"
            self._save_voice_modes()
            self._set_adapter_auto_tts_enabled(
                adapter, event.source.chat_id, enabled=True
            )
            try:
                await self._send_voice_reply(
                    event,
                    self._discord_voice_join_ack_text(
                        event.source,
                        adapter=adapter,
                        rejoining_persisted_voice_session=(
                            rejoining_persisted_voice_session
                        ),
                    ),
                )
            except Exception:
                logger.debug("Discord voice join greeting failed", exc_info=True)
            return (
                f"Joined voice channel **{voice_channel.name}**.\n"
                "I'll speak my replies and listen to you. Use /voice leave to "
                "disconnect."
            )
        adapter._voice_input_callback = None
        return "Failed to join voice channel. Check bot permissions (Connect + Speak)."

    async def _handle_voice_channel_leave(self, event: MessageEvent) -> str:
        """Leave the Discord voice channel."""
        adapter = self._adapter_for_source(event.source)
        guild_id = self._get_guild_id(event)
        if (
            not guild_id
            or adapter is None
            or not hasattr(adapter, "leave_voice_channel")
        ):
            return "Not in a voice channel."
        if (
            not hasattr(adapter, "is_in_voice_channel")
            or not adapter.is_in_voice_channel(guild_id)
        ):
            return "Not in a voice channel."
        try:
            await adapter.leave_voice_channel(guild_id)
        except Exception as exc:
            logger.warning("Error leaving voice channel: %s", exc)
        self._voice_mode[
            self._voice_key(event.source.platform, event.source.chat_id)
        ] = "off"
        self._save_voice_modes()
        self._set_adapter_auto_tts_disabled(
            adapter, event.source.chat_id, disabled=True
        )
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        return "Left voice channel."

    def _handle_voice_timeout_cleanup(self, chat_id: str, *, adapter=None) -> None:
        """Clean runner voice state for a timeout on the owning adapter."""
        self._voice_mode[self._voice_key(Platform.DISCORD, chat_id)] = "off"
        self._save_voice_modes()
        if adapter is None:
            adapter = self.adapters.get(Platform.DISCORD)
        self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)

    def _is_duplicate_voice_transcript(
        self, guild_id: int, user_id: int, transcript: str
    ) -> bool:
        """Suppress repeated STT outputs for the same recent utterance."""
        from difflib import SequenceMatcher

        normalized = re.sub(r"\s+", " ", transcript).strip().lower()
        normalized = re.sub(r"[^\w\s]", "", normalized)
        if not normalized:
            return False
        now = time.monotonic()
        window_seconds = 12.0
        key = (guild_id, user_id)
        recent_store = getattr(self, "_recent_voice_transcripts", None)
        if not isinstance(recent_store, dict):
            recent_store = {}
            self._recent_voice_transcripts = recent_store
        recent = [
            (timestamp, text)
            for timestamp, text in recent_store.get(key, [])
            if now - timestamp <= window_seconds
        ]
        for _, prior in recent:
            if prior == normalized:
                recent_store[key] = recent
                return True
            if (
                len(prior) >= 16
                and len(normalized) >= 16
                and SequenceMatcher(None, prior, normalized).ratio() >= 0.95
            ):
                recent_store[key] = recent
                return True
        recent.append((now, normalized))
        recent_store[key] = recent[-5:]
        return False

    async def _handle_voice_channel_input(
        self,
        guild_id: int,
        user_id: int,
        transcript: str,
        *,
        adapter=None,
    ):
        """Handle transcribed voice through the adapter that owns its profile."""
        if adapter is None:
            adapter = self.adapters.get(Platform.DISCORD)
        if not adapter:
            return
        text_channel_id = adapter._voice_text_channels.get(guild_id)
        if not text_channel_id:
            return

        source_data = getattr(adapter, "_voice_sources", {}).get(guild_id)
        if source_data:
            source = SessionSource.from_dict(source_data)
            source.user_id = str(user_id)
            source.user_name = str(user_id)
        else:
            source = SessionSource(
                platform=Platform.DISCORD,
                chat_id=str(text_channel_id),
                user_id=str(user_id),
                user_name=str(user_id),
                chat_type="group",
                profile=getattr(adapter, "_runtime_profile_name", None),
            )

        auto_voice_authorized = (
            str(source.chat_id)
            in getattr(adapter, "_auto_voice_session_channels", set())
            and hasattr(adapter, "_is_auto_voice_user_id_allowed")
            and adapter._is_auto_voice_user_id_allowed(source.user_id)
        )
        if not auto_voice_authorized and not self._is_user_authorized(source):
            logger.debug("Unauthorized voice input from user %d, ignoring", user_id)
            return

        try:
            from tools.voice_mode import stt_noise_drop_reason

            noise_reason = stt_noise_drop_reason(transcript)
        except Exception:
            noise_reason = None
        if noise_reason:
            logger.debug(
                "Dropping voice transcript before session injection "
                "(guild=%s user=%s reason=%s): %r",
                guild_id,
                user_id,
                noise_reason,
                transcript[:100],
            )
            return
        if self._is_duplicate_voice_transcript(guild_id, user_id, transcript):
            logger.info(
                "Suppressing duplicate voice transcript for guild=%s user=%s: %s",
                guild_id,
                user_id,
                transcript[:100],
            )
            return

        try:
            channel = adapter._client.get_channel(text_channel_id)
            if channel:
                safe_text = (
                    transcript[:2000]
                    .replace("@everyone", "@\u200beveryone")
                    .replace("@here", "@\u200bhere")
                )
                await channel.send(f"**[Voice]** <@{user_id}>: {safe_text}")
        except Exception:
            pass

        event = MessageEvent(
            source=source,
            text=transcript,
            message_type=MessageType.VOICE,
            raw_message=SimpleNamespace(guild_id=guild_id, guild=None),
        )
        await adapter.handle_message(event)
