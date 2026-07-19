"""Voice orchestration for the gateway runner.

``GatewayVoiceMixin`` holds the voice-channel lifecycle (join/leave/input),
voice-mode persistence, spoken interactive prompts (clarify/approval), voice
replies with TTS, and message transcription enrichment. ``GatewayRunner``
composes it exactly like the authorization/kanban/slash-command mixins; every
method here runs with ``self`` bound to the runner.
"""

import asyncio
import inspect
import json
import logging
import os
import random
import re
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from agent.async_utils import safe_schedule_threadsafe
from agent.i18n import t
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.temp_audio import gateway_tts_temp_path
from gateway.voice_acknowledgements import VoiceAcknowledgement

logger = logging.getLogger(__name__)

_DISCORD_VOICE_BUSY_ACK = "working, one sec"


async def _probe_audio_duration(path: str) -> Optional[str]:
    """Best-effort duration probe. Returns formatted MM:SS / HH:MM:SS, or None on failure."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".wav":
        try:
            def _wav_duration() -> float:
                import wave
                with wave.open(path, "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate() or 1
                    return frames / float(rate)
            secs = await asyncio.to_thread(_wav_duration)
            return _format_duration(secs)
        except Exception:
            pass

    if ext in (".ogg", ".opus", ".oga"):
        try:
            def _ogg_duration() -> float:
                from mutagen.oggopus import OggOpus
                return float(OggOpus(path).info.length)
            secs = await asyncio.to_thread(_ogg_duration)
            return _format_duration(secs)
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            return _format_duration(float(stdout.decode().strip()))
    except Exception:
        pass

    return None


class GatewayVoiceMixin:
    """Voice orchestration methods for ``GatewayRunner``."""

    def _voice_key(self, platform: Platform, chat_id: str) -> str:
        """Return a platform-namespaced key for voice mode state."""
        return f"{platform.value}:{chat_id}"

    def _load_voice_modes(self) -> Dict[str, str]:
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

        if not isinstance(data, dict):
            return {}

        valid_modes = {"off", "voice_only", "all"}
        result = {}
        for chat_id, mode in data.items():
            if mode not in valid_modes:
                continue
            key = str(chat_id)
            # Skip legacy unprefixed keys (warn and skip)
            if ":" not in key:
                logger.warning(
                    "Skipping legacy unprefixed voice mode key %r during migration. "
                    "Re-enable voice mode on that chat to rebuild the prefixed key.",
                    key,
                )
                continue
            result[key] = mode
        return result

    def _save_voice_modes(self) -> None:
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._VOICE_MODE_PATH.write_text(
                json.dumps(self._voice_mode, indent=2)
            )
        except OSError as e:
            logger.warning("Failed to save voice modes: %s", e)

    async def _wait_for_discord_voice_ready(
        self,
        adapter,
        guild_id: int,
        *,
        timeout: float = 3.0,
    ) -> bool:
        """Wait briefly for Discord voice to be connected before first playback.

        Discord.py can report the initial voice connection complete and then
        immediately enter a short reconnect/secret-refresh window. Auto-voice
        greetings generated during that window are valid TTS files, but the
        playback path sees no active VC and silently returns False. Waiting here
        makes the join acknowledgement use the same reliable voice path as later
        assistant replies.
        """
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

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice state into a live platform adapter.

        Populates three fields from config + ``self._voice_mode``:
          - ``_auto_tts_default``: global default from ``voice.auto_tts``
          - ``_auto_tts_enabled_chats``: chats with mode ``voice_only``/``all``
          - ``_auto_tts_disabled_chats``: chats with mode ``off``
        """
        platform = getattr(adapter, "platform", None)
        if not isinstance(platform, Platform):
            return

        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
        if not isinstance(disabled_chats, set) and not isinstance(enabled_chats, set):
            return

        # Push the global voice.auto_tts default (config.yaml) onto the adapter.
        # Lazy import to avoid adding a module-level dep from gateway → hermes_cli.
        try:
            from hermes_cli.config import load_config as _load_full_config
            _full_cfg = _load_full_config()
            _auto_tts_default = bool(
                (_full_cfg.get("voice") or {}).get("auto_tts", False)
            )
        except Exception:
            _auto_tts_default = False
        if hasattr(adapter, "_auto_tts_default"):
            adapter._auto_tts_default = _auto_tts_default

        prefix = f"{platform.value}:"
        if isinstance(disabled_chats, set):
            disabled_chats.clear()
            disabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode == "off" and key.startswith(prefix)
            )
        if isinstance(enabled_chats, set):
            enabled_chats.clear()
            enabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode in {"voice_only", "all"} and key.startswith(prefix)
            )

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
            logger.debug("Could not load voice-session history for join acknowledgement", exc_info=True)
            return 0
        return sum(1 for message in history if message.get("role") == "user")

    def _voice_ack_model_for_source(self, source: SessionSource) -> str:
        """Resolve the active LLM name used for acknowledgement filtering."""
        try:
            model, _runtime = self._resolve_session_agent_runtime(source=source)
            return str(model or "")
        except Exception:
            logger.debug("Could not resolve voice acknowledgement model", exc_info=True)
            return ""

    def _discord_voice_join_ack(
        self,
        source: SessionSource,
        *,
        adapter=None,
        rejoining_persisted_voice_session: bool = False,
    ):
        """Resolve the catalog or legacy acknowledgement for a Discord VC join."""
        try:
            session_key = self._session_key_for_source(source)
        except Exception:
            session_key = ""
        running_agents = getattr(self, "_running_agents", {}) or {}
        if session_key and session_key in running_agents:
            event_name = "busy"
            legacy_key = "busy_ack_phrases"
            fallback = _DISCORD_VOICE_BUSY_ACK
        elif rejoining_persisted_voice_session:
            event_name = "restart_join"
            legacy_key = "restart_join_ack_phrases"
            fallback = "Back online."
        else:
            threshold = self._configured_discord_voice_int(
                adapter, "session_resume_user_turn_threshold", 2
            )
            if self._voice_session_user_turn_count(source) > threshold:
                event_name = "session_resume"
                legacy_key = "session_resume_ack_phrases"
                fallback = "Picking up where we left off."
            else:
                event_name = "join"
                legacy_key = "join_ack_phrases"
                fallback = t("gateway.voice.connected_spoken")

        catalog = vars(adapter).get("_voice_ack_catalog") if adapter is not None else None
        selected = None
        if catalog:
            selected = catalog.choose(
                event_name,
                model_name=self._voice_ack_model_for_source(source),
            )
        if selected is not None:
            return selected

        return VoiceAcknowledgement(
            text=self._configured_discord_voice_ack(adapter, legacy_key) or fallback,
            weight=1,
            voice_settings={},
            include_models=("*",),
            exclude_models=(),
        )

    def _discord_voice_join_ack_text(
        self,
        source: SessionSource,
        *,
        adapter=None,
        rejoining_persisted_voice_session: bool = False,
    ) -> str:
        """Return only the spoken text for compatibility with existing callers."""
        return self._discord_voice_join_ack(
            source,
            adapter=adapter,
            rejoining_persisted_voice_session=rejoining_persisted_voice_session,
        ).text

    @staticmethod
    def _discord_adapter_connected_to_voice_channel(adapter, guild_id: int, channel_id: int) -> bool:
        """Return True when Discord is already connected to ``channel_id`` in ``guild_id``."""
        voice_clients = getattr(adapter, "_voice_clients", {}) or {}
        existing = voice_clients.get(int(guild_id)) if isinstance(voice_clients, dict) else None
        if existing is None:
            return False
        try:
            if not existing.is_connected():
                return False
        except Exception:
            return False
        existing_channel = getattr(existing, "channel", None)
        return getattr(existing_channel, "id", None) == int(channel_id)

    async def _handle_discord_auto_voice_join(self, adapter, member, voice_channel) -> bool:
        """Join the configured Discord voice channel when an authorized user enters it."""
        guild = getattr(voice_channel, "guild", None) or getattr(member, "guild", None)
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            return False
        channel_id = getattr(voice_channel, "id", None)
        was_connected_to_channel = (
            channel_id is not None
            and self._discord_adapter_connected_to_voice_channel(
                adapter,
                int(guild_id),
                int(channel_id),
            )
        )
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = self._handle_voice_channel_input
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = self._handle_voice_timeout_cleanup

        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as e:
            logger.warning("Failed to auto-join Discord voice channel: %s", e)
            self._clear_discord_voice_callbacks_if_idle(adapter)
            return False
        if not success:
            self._clear_discord_voice_callbacks_if_idle(adapter)
            return False

        # The session always anchors to the voice channel's own text chat —
        # transcripts and text replies land where the conversation happens.
        session_channel_id = int(getattr(voice_channel, "id"))
        chat_id = str(session_channel_id)
        rejoining_persisted_voice_session = self._voice_mode.get(
            self._voice_key(Platform.DISCORD, chat_id)
        ) in {"voice_only", "all"}
        adapter._voice_text_channels[int(guild_id)] = session_channel_id
        if hasattr(adapter, "_auto_voice_session_channels"):
            adapter._auto_voice_session_channels.add(chat_id)
        if hasattr(adapter, "_voice_sources"):
            voice_source = SessionSource(
                platform=Platform.DISCORD,
                chat_id=chat_id,
                chat_name=getattr(voice_channel, "name", None),
                chat_type="group",
                user_id=str(getattr(member, "id", "")) or None,
                user_name=getattr(member, "display_name", None),
                guild_id=str(guild_id),
            )
            adapter._voice_sources[int(guild_id)] = voice_source.to_dict()
        else:
            voice_source = SessionSource(
                platform=Platform.DISCORD,
                chat_id=chat_id,
                chat_name=getattr(voice_channel, "name", None),
                chat_type="group",
                user_id=str(getattr(member, "id", "")) or None,
                user_name=getattr(member, "display_name", None),
                guild_id=str(guild_id),
            )
        self._voice_mode[self._voice_key(Platform.DISCORD, chat_id)] = "voice_only"
        self._save_voice_modes()
        self._set_adapter_auto_tts_enabled(adapter, chat_id, enabled=True)
        if was_connected_to_channel:
            return True
        try:
            if not await self._wait_for_discord_voice_ready(adapter, int(guild_id)):
                logger.warning(
                    "Discord auto-voice greeting skipped: voice connection not ready "
                    "for guild %s",
                    guild_id,
                )
                return True
            auto_voice_event = MessageEvent(
                source=voice_source,
                text="",
                message_type=MessageType.TEXT,
                raw_message=SimpleNamespace(guild_id=int(guild_id), guild=guild),
            )
            auto_voice_ack = self._discord_voice_join_ack(
                voice_source,
                adapter=adapter,
                rejoining_persisted_voice_session=rejoining_persisted_voice_session,
            )
            auto_voice_kwargs = (
                {"voice_settings": auto_voice_ack.voice_settings}
                if auto_voice_ack.voice_settings
                else {}
            )
            await self._send_voice_reply(
                auto_voice_event,
                auto_voice_ack.text,
                **auto_voice_kwargs,
            )
        except Exception:
            logger.debug("Discord auto-voice greeting failed", exc_info=True)
        logger.info(
            "Auto-joined Discord voice channel %s (%s) for user %s",
            getattr(voice_channel, "name", "?"),
            getattr(voice_channel, "id", "?"),
            getattr(member, "id", "?"),
        )
        return True

    async def _handle_discord_auto_voice_leave(self, adapter, member, voice_channel) -> bool:
        """Leave the configured Discord voice channel when authorized users leave it."""
        guild = getattr(voice_channel, "guild", None) or getattr(member, "guild", None)
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            return False
        text_channel_id = getattr(adapter, "_voice_text_channels", {}).get(int(guild_id))
        if text_channel_id is None:
            text_channel_id = getattr(voice_channel, "id", None)

        try:
            await adapter.leave_voice_channel(int(guild_id))
        except Exception as e:
            logger.warning("Failed to auto-leave Discord voice channel: %s", e)

        if text_channel_id is not None:
            chat_id = str(text_channel_id)
            self._voice_mode[self._voice_key(Platform.DISCORD, chat_id)] = "off"
            self._save_voice_modes()
            self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
            if hasattr(adapter, "_auto_voice_session_channels"):
                adapter._auto_voice_session_channels.discard(chat_id)
        self._clear_discord_voice_callbacks_if_idle(adapter)
        logger.info(
            "Auto-left Discord voice channel %s (%s) after user %s left",
            getattr(voice_channel, "name", "?"),
            getattr(voice_channel, "id", "?"),
            getattr(member, "id", "?"),
        )
        return True

    def _clear_discord_voice_callbacks_if_idle(self, adapter) -> None:
        """Clear adapter-wide Discord voice callbacks only when no VC remains active."""
        voice_clients = getattr(adapter, "_voice_clients", {}) or {}
        if voice_clients:
            return
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = None

    async def _handle_voice_channel_join(self, event: MessageEvent) -> str:
        """Join the user's current Discord voice channel."""
        adapter = self.adapters.get(event.source.platform)
        if not hasattr(adapter, "join_voice_channel"):
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

        # Wire callbacks BEFORE join so voice input arriving immediately
        # after connection is not lost.
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = self._handle_voice_channel_input
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = self._handle_voice_timeout_cleanup
        # Let the adapter's inactivity timer see the live voice-reply mode so it
        # doesn't disconnect a deliberately text-only (/voice off) session.
        if hasattr(adapter, "_voice_mode_getter"):
            adapter._voice_mode_getter = lambda chat_id: self._voice_mode.get(
                self._voice_key(Platform.DISCORD, str(chat_id)), "off"
            )

        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as e:
            logger.warning("Failed to join voice channel: %s", e)
            adapter._voice_input_callback = None
            err_lower = str(e).lower()
            if "pynacl" in err_lower or "nacl" in err_lower or "davey" in err_lower:
                return (
                    "Voice dependencies are missing (PyNaCl / davey). "
                    f"Install with: `{sys.executable} -m pip install PyNaCl`"
                )
            return f"Failed to join voice channel: {e}"

        if success:
            adapter._voice_text_channels[guild_id] = int(event.source.chat_id)
            if hasattr(adapter, "_voice_sources"):
                adapter._voice_sources[guild_id] = event.source.to_dict()
            self._voice_mode[self._voice_key(event.source.platform, event.source.chat_id)] = "voice_only"
            self._save_voice_modes()
            self._set_adapter_auto_tts_enabled(adapter, event.source.chat_id, enabled=True)
            try:
                join_ack = self._discord_voice_join_ack(
                    event.source,
                    adapter=adapter,
                    rejoining_persisted_voice_session=rejoining_persisted_voice_session,
                )
                join_voice_kwargs = (
                    {"voice_settings": join_ack.voice_settings}
                    if join_ack.voice_settings
                    else {}
                )
                await self._send_voice_reply(
                    event,
                    join_ack.text,
                    **join_voice_kwargs,
                )
            except Exception:
                logger.debug("Discord voice join greeting failed", exc_info=True)
            return (
                f"Joined voice channel **{voice_channel.name}**.\n"
                f"I'll speak my replies and listen to you. Use /voice leave to disconnect."
            )
        # Join failed — clear callback
        adapter._voice_input_callback = None
        return "Failed to join voice channel. Check bot permissions (Connect + Speak)."

    async def _handle_voice_channel_leave(self, event: MessageEvent) -> str:
        """Leave the Discord voice channel."""
        adapter = self.adapters.get(event.source.platform)
        guild_id = self._get_guild_id(event)

        if not guild_id or not hasattr(adapter, "leave_voice_channel"):
            return "Not in a voice channel."

        if not hasattr(adapter, "is_in_voice_channel") or not adapter.is_in_voice_channel(guild_id):
            return "Not in a voice channel."

        try:
            await adapter.leave_voice_channel(guild_id)
        except Exception as e:
            logger.warning("Error leaving voice channel: %s", e)
        # Always clean up state even if leave raised an exception
        self._voice_mode[self._voice_key(event.source.platform, event.source.chat_id)] = "off"
        self._save_voice_modes()
        self._set_adapter_auto_tts_disabled(adapter, event.source.chat_id, disabled=True)
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        return "Left voice channel."

    def _handle_voice_timeout_cleanup(self, chat_id: str) -> None:
        """Called by the adapter when a voice channel times out.

        Cleans up runner-side voice_mode state that the adapter cannot reach.
        """
        self._voice_mode[self._voice_key(Platform.DISCORD, chat_id)] = "off"
        self._save_voice_modes()
        adapter = self.adapters.get(Platform.DISCORD)
        self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)

    def _is_duplicate_voice_transcript(self, guild_id: int, user_id: int, transcript: str) -> bool:
        """Suppress repeated STT outputs for the same recent utterance.

        Voice capture can occasionally emit the same utterance twice a few
        seconds apart, which creates a second queued agent run and overlapping
        spoken replies. Dedup exact and near-exact repeats per guild/user over a
        short window while allowing genuinely new turns through.
        """
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
            (ts, txt)
            for ts, txt in recent_store.get(key, [])
            if now - ts <= window_seconds
        ]

        for _, prior in recent:
            if prior == normalized:
                recent_store[key] = recent
                return True
            if len(prior) >= 16 and len(normalized) >= 16:
                if SequenceMatcher(None, prior, normalized).ratio() >= 0.95:
                    recent_store[key] = recent
                    return True

        recent.append((now, normalized))
        recent_store[key] = recent[-5:]
        return False

    async def _handle_voice_channel_input(
        self, guild_id: int, user_id: int, transcript: str
    ):
        """Handle transcribed voice from a user in a voice channel.

        Creates a synthetic MessageEvent and processes it through the
        adapter's full message pipeline (session, typing, agent, TTS reply).
        """
        adapter = self.adapters.get(Platform.DISCORD)
        if not adapter:
            return

        session_ch_id = adapter._voice_text_channels.get(guild_id)
        if not session_ch_id:
            return

        # Build source — reuse the linked text channel's metadata when available
        # so voice input shares the same session as the bound text conversation.
        source_data = getattr(adapter, "_voice_sources", {}).get(guild_id)
        if source_data:
            source = SessionSource.from_dict(source_data)
            source.user_id = str(user_id)
            source.user_name = str(user_id)
        else:
            source = SessionSource(
                platform=Platform.DISCORD,
                chat_id=str(session_ch_id),
                user_id=str(user_id),
                user_name=str(user_id),
                chat_type="group",
            )

        auto_voice_authorized = False
        if source.platform == Platform.DISCORD and hasattr(adapter, "_is_auto_voice_user_id_allowed"):
            auto_session_channels = getattr(adapter, "_auto_voice_session_channels", set())
            auto_voice_authorized = (
                isinstance(auto_session_channels, set)
                and str(source.chat_id) in auto_session_channels
                and adapter._is_auto_voice_user_id_allowed(source.user_id)
            )

        # Check authorization before processing voice input.
        # Explicit auto-voice users are allowed to speak in the configured VC
        # even if they are not part of the broader Discord text allowlist.
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

        # Echo the transcript into the session channel (the voice channel's chat).
        transcript_ch_id = getattr(adapter, "_voice_text_channels", {}).get(guild_id)
        if transcript_ch_id:
            try:
                channel = adapter._client.get_channel(transcript_ch_id)
                if channel:
                    safe_text = transcript[:2000].replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
                    await channel.send(f"**[Voice]** <@{user_id}>: {safe_text}")
            except Exception:
                pass

        # Build a synthetic MessageEvent and feed through the normal pipeline
        # Use SimpleNamespace as raw_message so _get_guild_id() can extract
        # guild_id and _send_voice_reply() plays audio in the voice channel.
        from types import SimpleNamespace
        event = MessageEvent(
            source=source,
            text=transcript,
            message_type=MessageType.VOICE,
            raw_message=SimpleNamespace(guild_id=guild_id, guild=None),
        )

        await adapter.handle_message(event)

    def _format_clarify_prompt_for_tts(self, question: str, choices: Optional[list]) -> str:
        """Build a concise spoken version of a clarify prompt.

        The visual clarify UI may use buttons or numbered text. Voice mode needs
        the same prompt spoken explicitly because the clarify callback bypasses
        the normal assistant-response delivery path.
        """
        clean_question = " ".join(str(question or "").split())
        clean_choices = [" ".join(str(choice).split()) for choice in (choices or [])]
        clean_choices = [choice for choice in clean_choices if choice]

        if not clean_choices:
            return f"I need clarification: {clean_question}"

        parts = [f"I need a choice: {clean_question}"]
        parts.extend(
            f"Option {index}: {choice}."
            for index, choice in enumerate(clean_choices, start=1)
        )
        parts.append("Or say another answer.")
        return " ".join(parts)

    @staticmethod
    def _format_approval_prompt_for_tts(description: str) -> str:
        """Build a concise spoken notification for a command-approval prompt."""
        clean_description = " ".join(str(description or "dangerous command").split())
        return (
            "Command approval needed. "
            f"Reason: {clean_description}. "
            "Use the approval buttons, or say approve or deny."
        )

    def _maybe_resolve_voice_approval_response(self, event: MessageEvent) -> bool:
        """Resolve a pending command approval from a standalone spoken response.

        Approval prompts explicitly invite the user to say ``approve`` or
        ``deny``. Voice transcripts otherwise enter the ordinary message
        pipeline, where they cannot reach the agent thread blocked in
        ``tools.approval``. The accepted token may have terminal STT
        punctuation, but no extra words are accepted: unlike a clarify
        response, an accidental affirmative must not authorize a destructive
        command.
        """
        if event.message_type != MessageType.VOICE:
            return False

        response = (event.text or "").strip().casefold().rstrip(".!?")
        choice = {
            "approve": "once",
            "approved": "once",
            "allow": "once",
            "deny": "deny",
        }.get(response)
        if choice is None:
            return False

        try:
            from tools.approval import has_blocking_approval, resolve_gateway_approval

            session_key = self._session_key_for_source(event.source)
            if not has_blocking_approval(session_key):
                return False
            resolved = resolve_gateway_approval(session_key, choice)
        except Exception:
            logger.exception("Voice approval response handling failed")
            return False

        if resolved:
            logger.info(
                "Gateway resolved a pending command approval from voice (session=%s, choice=%s)",
                session_key,
                choice,
            )
            return True
        return False

    def _should_send_interactive_prompt_voice(self, event: MessageEvent) -> bool:
        """Return whether a direct interactive prompt should be spoken."""
        chat_id = event.source.chat_id
        voice_mode = self._voice_mode.get(
            self._voice_key(event.source.platform, chat_id), "off"
        )
        is_voice_input = (event.message_type == MessageType.VOICE)
        return voice_mode == "all" or (voice_mode == "voice_only" and is_voice_input)

    def _should_send_clarify_voice_prompt(self, event: MessageEvent) -> bool:
        """Return whether a clarify prompt should be spoken for this event."""
        return self._should_send_interactive_prompt_voice(event)

    def _schedule_interactive_prompt_voice(
        self,
        *,
        event: MessageEvent,
        spoken_prompt: str,
        loop,
        log_message: str,
    ) -> bool:
        """Schedule best-effort TTS for a prompt that bypasses final replies."""
        if not self._should_send_interactive_prompt_voice(event):
            return False
        if not str(spoken_prompt or "").strip():
            return False

        fut = safe_schedule_threadsafe(
            self._send_voice_reply(event, spoken_prompt),
            loop,
            logger=logger,
            log_message=log_message,
        )
        return fut is not None

    def _maybe_send_clarify_voice_prompt(
        self,
        *,
        event: MessageEvent,
        question: str,
        choices: Optional[list],
        loop,
    ) -> bool:
        """Schedule a spoken clarify prompt when voice mode is active.

        This is best-effort: the text/button clarify prompt remains the source
        of truth, and TTS failures must not cancel the pending clarify waiter.
        """
        return self._schedule_interactive_prompt_voice(
            event=event,
            spoken_prompt=self._format_clarify_prompt_for_tts(question, choices),
            loop=loop,
            log_message="Clarify voice prompt failed to schedule",
        )

    def _maybe_send_approval_voice_prompt(
        self,
        *,
        event: MessageEvent,
        description: str,
        loop,
    ) -> bool:
        """Schedule a spoken command-approval notification in voice mode."""
        return self._schedule_interactive_prompt_voice(
            event=event,
            spoken_prompt=self._format_approval_prompt_for_tts(description),
            loop=loop,
            log_message="Approval voice prompt failed to schedule",
        )

    def _should_send_voice_reply(
        self,
        event: MessageEvent,
        response: str,
        agent_messages: list,
        already_sent: bool = False,
    ) -> bool:
        """Decide whether the runner should send a TTS voice reply.

        Returns False when:
        - voice_mode is off for this chat
        - response is empty or an error
        - agent already called text_to_speech tool (dedup)
        - voice input and base adapter auto-TTS already handled it (skip_double)
          UNLESS streaming already consumed the response (already_sent=True),
          in which case the base adapter won't have text for auto-TTS so the
          runner must handle it.
        """
        if not response or response.startswith("Error:"):
            return False

        chat_id = event.source.chat_id
        voice_mode = self._voice_mode.get(self._voice_key(event.source.platform, chat_id), "off")
        is_voice_input = (event.message_type == MessageType.VOICE)

        should = (
            (voice_mode == "all")
            or (voice_mode == "voice_only" and is_voice_input)
        )
        if not should:
            return False

        # Dedup: agent already called TTS tool
        has_agent_tts = any(
            msg.get("role") == "assistant"
            and any(
                tc.get("function", {}).get("name") == "text_to_speech"
                for tc in (msg.get("tool_calls") or [])
            )
            for msg in agent_messages
        )
        if has_agent_tts:
            return False

        # Dedup: base adapter auto-TTS already handles voice input
        # (play_tts plays in VC when connected, so runner can skip).
        # When streaming already delivered the text (already_sent=True),
        # the base adapter will receive None and can't run auto-TTS,
        # so the runner must take over.
        if is_voice_input and not already_sent:
            return False

        return True

    async def _send_voice_reply(
        self,
        event: MessageEvent,
        text: str,
        *,
        voice_settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Generate TTS audio and send as a voice message before the text reply."""
        audio_path = None
        actual_path = None
        try:
            with self._runtime_scope_for_source(event.source):
                from tools.tts_tool import text_to_speech_tool, _strip_markdown_for_tts

                tts_text = _strip_markdown_for_tts(text[:4000])
                if not tts_text:
                    return

                # Telegram's adapter only sends native voice bubbles for OGG/Opus.
                # Other platforms keep the existing MP3 default.
                audio_ext = "ogg" if event.source.platform == Platform.TELEGRAM else "mp3"
                audio_path = gateway_tts_temp_path("tts_reply", audio_ext)
                os.makedirs(os.path.dirname(audio_path), exist_ok=True)

                tts_kwargs = {"text": tts_text, "output_path": audio_path}
                if voice_settings:
                    tts_kwargs["voice_settings"] = voice_settings
                result_json = await asyncio.to_thread(
                    text_to_speech_tool,
                    **tts_kwargs,
                )
            try:
                result = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Auto voice reply TTS returned invalid JSON: %s", result_json[:200] if result_json else result_json)
                return

            # Use the actual file path from result (may differ after opus conversion)
            actual_path = result.get("file_path", audio_path)
            if not result.get("success") or not os.path.isfile(actual_path):
                logger.warning("Auto voice reply TTS failed: %s", result.get("error"))
                return

            adapter = self.adapters.get(event.source.platform)
            reply_anchor = self._reply_anchor_for_event(event)
            thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
            # Mark the auto voice reply as notify-worthy.  Mirrors the
            # final-text path in gateway/platforms/base.py which sets
            # ``notify=True`` so platform adapters that gate push
            # notifications (Telegram "important" mode) deliver the
            # final voice reply as a normal notification instead of a
            # silent message.  Clone first so we don't mutate metadata
            # shared with concurrent typing-indicator state.
            if thread_meta is not None:
                thread_meta = dict(thread_meta)
                thread_meta["notify"] = True
            else:
                thread_meta = {"notify": True}

            # If connected to a voice channel, play there instead of sending a file.
            guild_id = self._get_guild_id(event)
            if (guild_id
                    and hasattr(adapter, "play_in_voice_channel")
                    and hasattr(adapter, "is_in_voice_channel")
                    and adapter.is_in_voice_channel(guild_id)):
                await adapter.play_in_voice_channel(guild_id, actual_path)
            elif adapter and hasattr(adapter, "play_tts"):
                await adapter.play_tts(
                    chat_id=event.source.chat_id,
                    audio_path=actual_path,
                    reply_to=reply_anchor,
                    metadata=thread_meta,
                )
            elif adapter and hasattr(adapter, "send_voice"):
                send_kwargs: Dict[str, Any] = {
                    "chat_id": event.source.chat_id,
                    "audio_path": actual_path,
                    "reply_to": reply_anchor,
                    "metadata": thread_meta,
                }
                await adapter.send_voice(**send_kwargs)
        except Exception as e:
            logger.warning("Auto voice reply failed: %s", e, exc_info=True)
        finally:
            for p in {audio_path, actual_path} - {None}:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    async def _enrich_message_with_transcription(
        self,
        user_text: str,
        audio_paths: List[str],
    ) -> tuple[str, List[str]]:
        """
        Auto-transcribe user voice/audio messages using the configured STT provider
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            A tuple of ``(enriched_text, successful_transcripts)``:
              - ``enriched_text``: the message string with transcription wrappers
                prepended (same as before).
              - ``successful_transcripts``: the raw transcript strings for audio
                clips that were successfully transcribed, in input order. Empty
                list if every clip failed or STT is disabled. Callers can use
                this to echo transcripts back to the user before the agent loop.
        """
        if not getattr(self.config, "stt_enabled", True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await _probe_audio_duration(abs_path)
                if duration_str:
                    notes.append(
                        f"[The user sent a voice message: {abs_path} (duration: {duration_str})]"
                    )
                else:
                    notes.append(f"[The user sent a voice message: {abs_path}]")
            if not notes:
                return user_text, []
            prefix = "\n\n".join(notes)
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, []
            if user_text:
                return f"{prefix}\n\n{user_text}", []
            return prefix, []

        from tools.transcription_tools import transcribe_audio
        from tools.voice_mode import clean_voice_transcript

        enriched_parts = []
        successful_transcripts: List[str] = []
        for path in audio_paths:
            try:
                logger.debug("Transcribing user voice: %s", path)
                result = await asyncio.to_thread(transcribe_audio, path)
                if result["success"]:
                    transcript = clean_voice_transcript(result["transcript"])
                    successful_transcripts.append(transcript)
                    enriched_parts.append(
                        f'**[Voice]** [The user sent a voice message~ '
                        f'Here\'s what they said: "{transcript}"]'
                    )
                else:
                    error = result.get("error", "unknown error")
                    if (
                        "No STT provider" in error
                        or error.startswith("Neither VOICE_TOOLS_OPENAI_KEY nor OPENAI_API_KEY is set")
                    ):
                        _no_stt_note = (
                            "[The user sent a voice message but I can't listen "
                            "to it right now — no STT provider is configured. "
                            "A direct message has already been sent to the user "
                            "with setup instructions."
                        )
                        if self._has_setup_skill():
                            _no_stt_note += (
                                " You have a skill called hermes-agent-setup "
                                "that can help users configure Hermes features "
                                "including voice, tools, and more."
                            )
                        _no_stt_note += "]"
                        enriched_parts.append(_no_stt_note)
                    else:
                        enriched_parts.append(
                            "[The user sent a voice message but I had trouble "
                            f"transcribing it~ ({error})]"
                        )
            except Exception as e:
                logger.error("Transcription error: %s", e)
                enriched_parts.append(
                    "[The user sent a voice message but something went wrong "
                    "when I tried to listen to it~ Let them know!]"
                )

        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            # Strip the empty-content placeholder from the Discord adapter
            # when we successfully transcribed the audio — it's redundant.
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, successful_transcripts
            if user_text:
                return f"{prefix}\n\n{user_text}", successful_transcripts
            return prefix, successful_transcripts
        return user_text, successful_transcripts

    async def _dequeue_pending_with_transcription(
        self,
        adapter,
        session_key: str,
        source,
    ) -> str | None:
        """Dequeue a pending queued message, auto-transcribing audio media.

        When a voice/audio message arrives during an active agent run, the
        adapter stores the event in its pending queue and signals an interrupt
        (see base.BaseAdapter.handle_message). The adapter path bypasses
        _handle_message entirely, so the normal STT pipeline at message-receive
        time never runs.

        This helper fills that gap: when the dequeued event has audio media,
        we transcribe inline, echo the raw transcript back to the user (same
        "🎙️" format as the fresh-message path), and return enriched text.
        Non-audio events fall back to _build_media_placeholder, matching the
        original _dequeue_pending_text behavior.
        """
        from gateway.run import _build_media_placeholder
        event = adapter.get_pending_message(session_key)
        if not event:
            return None

        text = event.text or ""

        audio_paths: List[str] = []
        media_urls = getattr(event, "media_urls", None) or []
        media_types = getattr(event, "media_types", None) or []
        for i, path in enumerate(media_urls):
            mtype = media_types[i] if i < len(media_types) else ""
            is_audio = (
                mtype.startswith("audio/")
                or getattr(event, "message_type", None) in (MessageType.VOICE, MessageType.AUDIO)
            )
            if is_audio:
                audio_paths.append(path)

        if audio_paths:
            enriched_text, successful_transcripts = await self._enrich_message_with_transcription(
                text, audio_paths,
            )
            # Echo raw transcripts back to the user so voice interrupts
            # feel identical to fresh voice messages.
            if successful_transcripts:
                echo_adapter = self.adapters.get(source.platform)
                echo_meta = {"thread_id": source.thread_id} if source.thread_id else None
                if echo_adapter:
                    for tx in successful_transcripts:
                        try:
                            await echo_adapter.send(
                                source.chat_id,
                                f'🎙️ "{tx}"',
                                metadata=echo_meta,
                            )
                        except Exception as echo_exc:
                            logger.debug(
                                "Transcript echo failed (non-fatal): %s", echo_exc,
                            )
            return enriched_text or None

        # Non-audio fallback: preserve original _dequeue_pending_text semantics.
        if not text and media_urls:
            text = _build_media_placeholder(event)
        return text or None
