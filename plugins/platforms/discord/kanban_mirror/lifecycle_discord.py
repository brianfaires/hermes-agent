"""Concrete Discord publisher for durable terminal lifecycles."""
from __future__ import annotations
import hashlib
from datetime import datetime
from .discord_client import DiscordClient, ensure_forum_tags
from .lifecycle import PublishReceipt, _hash
from .state import get_digest


class DiscordLifecyclePublisher:
    def __init__(self, client: DiscordClient, cfg, conn):
        self.client, self.cfg, self.conn = client, cfg, conn

    def _receipt(self, key, thread, payload, object_id):
        return PublishReceipt(key, thread, _hash(payload), str(object_id or ""))

    def publish_summary(self, thread_id, payload, *, operation_key):
        lines = ["**Final summary**"]
        dates = payload.get("date_range", {})
        lines.append(f"Date: {dates.get('start') or '?'} — {dates.get('end') or '?'}")
        for card in payload.get("card_chain", []):
            lines.append(f"- **{card.get('title') or card.get('task_id')}** — {card.get('status')}")
        for outcome in payload.get("outcomes", []):
            if outcome.get("outcome"):
                lines.append(f"  Outcome: {outcome['outcome']}")
        if payload.get("owners"):
            lines.append("Owners: " + ", ".join(map(str, payload["owners"])))
        nonce = hashlib.sha256(operation_key.encode()).hexdigest()[:25]
        response = self.client.send_message(thread_id, content="\n".join(lines), nonce=nonce)
        return self._receipt(operation_key, thread_id, payload, response.get("id"))

    def upsert_digest(self, thread_id, payload, *, operation_key):
        digest = get_digest(self.conn)
        if digest is None or not digest.thread_id or not digest.starter_message_id:
            raise ValueError("terminal lifecycle requires an existing digest thread")
        dates = payload.get("date_range", {})
        marker = f"<!-- terminal:{thread_id} -->"
        block = marker + "\n" + f"- [{dates.get('end') or dates.get('start') or '?'}]({payload.get('thread_link')}) — {payload.get('outcome') or 'completed'}"
        old = str(self.client.get_message(digest.thread_id, digest.starter_message_id).get("content") or "")
        lines = old.splitlines()
        start = next((i for i, line in enumerate(lines) if line == marker), None)
        content = (old.rstrip() + "\n\n" + block).strip() if start is None else "\n".join(lines[:start] + block.splitlines() + lines[start + 2:])
        response = self.client.update_message(digest.thread_id, digest.starter_message_id, content=content)
        self.client.update_thread(digest.thread_id, pinned=True)
        live_message = self.client.get_message(digest.thread_id, digest.starter_message_id)
        live_thread = self.client.get_channel(digest.thread_id)
        pinned = bool(live_thread.get("pinned") or (int(live_thread.get("flags") or 0) & 2))
        if block not in str(live_message.get("content") or "") or not pinned:
            raise ValueError("digest content/pin not verified")
        return self._receipt(operation_key, thread_id, payload, response.get("id") or digest.starter_message_id)

    def apply_done_tag(self, thread_id, payload, *, operation_key):
        forum = self.client.get_channel(self.cfg.forum_channel_id)
        lookup, _ = ensure_forum_tags(self.client, forum, ["done"])
        channel = self.client.get_channel(thread_id)
        tags = list(channel.get("applied_tags", []))
        done_id = lookup["done"]
        if done_id not in tags:
            tags.append(done_id)
        response = self.client.update_thread(thread_id, tag_ids=tags)
        return self._receipt(operation_key, thread_id, payload, response.get("id") or thread_id)

    def archive_thread(self, thread_id, payload, *, operation_key):
        response = self.client.update_thread(thread_id, archive=True)
        return self._receipt(operation_key, thread_id, payload, response.get("id") or thread_id)

    def read_thread_state(self, thread_id):
        channel = self.client.get_channel(thread_id)
        forum = self.client.get_channel(self.cfg.forum_channel_id)
        done_ids = {str(t.get("id")) for t in forum.get("available_tags", []) if str(t.get("name", "")).lower() == "done"}
        latest = 0
        message_id = str(channel.get("last_message_id") or "")
        if message_id:
            raw = str(self.client.get_message(thread_id, message_id).get("timestamp") or "")
            if raw:
                latest = int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        metadata = channel.get("thread_metadata") or {}
        return {"done": bool(done_ids.intersection(map(str, channel.get("applied_tags", [])))),
                "archived": bool(metadata.get("archived", channel.get("archived", False))),
                "latest_activity_at": latest}
