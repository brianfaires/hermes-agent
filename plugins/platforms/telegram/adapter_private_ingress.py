"""Private text before Telegram observation, media resolution and split batching."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


class TelegramTextIngressMixin:
    def _private_telegram_match(self, message):
        from gateway.private_commands import private_home_for_source
        from hermes_cli.private_commands import match_private_command
        text = getattr(message, 'text', None) or getattr(message, 'caption', None) or ''
        if not text.lstrip().startswith('/'):
            return False
        user = getattr(message, 'from_user', None)
        chat = message.chat
        source = self.build_source(chat_id=str(chat.id), user_id=str(user.id) if user else None,
            chat_type='dm' if self._chat_type_str(chat) == 'private' else 'group',
            thread_id=self._effective_message_thread_id(message))
        return match_private_command(text, home=private_home_for_source(source)) is not None

    async def _private_text_ingress(self, msg, update, *, admitted=False):
        if not self._private_telegram_match(msg):
            return False
        # Command ingress already passed the native policy. Text/media ingress
        # must apply it before capture or a private rejection response, without
        # observing the body of a rejected private command.
        if not admitted and not self._should_process_message(msg, is_command=True):
            return True
        from plugins.platforms.telegram.adapter import MessageType
        event = self._build_message_event(msg, MessageType.COMMAND, update_id=update.update_id)
        event.text = msg.text or msg.caption or ''
        if msg.caption:
            event.message_type = MessageType.DOCUMENT
        return await self._dispatch_private_event(event)

    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text; buffers client-split chunks into one MessageEvent."""
        from plugins.platforms.telegram.adapter import MessageType
        msg = self._effective_update_message(update)
        if not msg or not msg.text:
            return
        # Auth check first: blocked users must not reach batching, the observed transcript, or the agent.
        if not self._is_user_authorized_from_message(msg):
            self._log_blocked_user(msg)
            return
        if await self._private_text_ingress(msg, update):
            return
        if not self._gate_or_observe(msg, update, MessageType.TEXT):
            return
        await self._ensure_forum_commands(update.message)
        self._enqueue_text_event(await self._build_triggered_event(msg, update, MessageType.TEXT))

    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming command messages."""
        from plugins.platforms.telegram.adapter import MessageType
        msg = self._effective_update_message(update)
        if not msg or not msg.text:
            return
        if not self._should_process_message(msg, is_command=True):
            return
        if not self._is_user_authorized_from_message(msg):
            self._log_blocked_user(msg)
            return
        if await self._private_text_ingress(msg, update, admitted=True):
            return
        await self._ensure_forum_commands(msg)
        event = await self._build_triggered_event(msg, update, MessageType.COMMAND)
        # A >4096-char command paste arrives as a near-limit COMMAND chunk plus TEXT continuations; dispatching
        # immediately would orphan them. Near-limit commands go through text batching.
        if len(event.text or "") >= self._SPLIT_THRESHOLD:
            self._enqueue_text_event(event)
            return
        await self.handle_message(event)
