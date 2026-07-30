"""Concrete Discord publisher for durable terminal lifecycles."""
from __future__ import annotations
import hashlib
from datetime import datetime
from .discord_client import DiscordClient, ensure_forum_tags
from .lifecycle import PublishReceipt, _hash
from .state import get_digest


_DISCORD_MESSAGE_LIMIT = 2000
_WORKFLOW_TAG_NAMES = {"running", "review", "waiting", "done", "needs-brian"}


def _bounded_digest_content(old: str, marker: str, block: str) -> str:
    """Keep the digest as a rolling index without exceeding Discord's limit."""
    lines = str(old or "").splitlines()
    base: list[str] = []
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("<!-- terminal:") and line.endswith(" -->"):
            entry = "\n".join(lines[index:index + 2]).strip()
            if line != marker:
                blocks.append(entry)
            index += 2
            continue
        base.append(line)
        index += 1
    blocks.append(block)

    def render() -> str:
        sections = ["\n".join(base).strip(), *blocks]
        return "\n\n".join(section for section in sections if section).strip()

    content = render()
    while len(content) > _DISCORD_MESSAGE_LIMIT and len(blocks) > 1:
        blocks.pop(0)
        content = render()
    if len(content) > _DISCORD_MESSAGE_LIMIT:
        raise ValueError("terminal digest base content leaves no room for one entry")
    return content


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
        content = _bounded_digest_content(old, marker, block)
        response = self.client.update_message(digest.thread_id, digest.starter_message_id, content=content)
        self.client.update_thread(digest.thread_id, pinned=True)
        live_message = self.client.get_message(digest.thread_id, digest.starter_message_id)
        live_thread = self.client.get_channel(digest.thread_id)
        pinned = bool(live_thread.get("pinned") or (int(live_thread.get("flags") or 0) & 2))
        if block not in str(live_message.get("content") or "") or not pinned:
            raise ValueError("digest content/pin not verified")
        return self._receipt(operation_key, thread_id, payload, response.get("id") or digest.starter_message_id)

    def upsert_digest_batch(self, entries):
        """Publish many terminal entries with one edit of the old digest message."""
        digest = get_digest(self.conn)
        if digest is None or not digest.thread_id or not digest.starter_message_id:
            raise ValueError("terminal lifecycle requires an existing digest thread")
        old = str(self.client.get_message(digest.thread_id, digest.starter_message_id).get("content") or "")
        content = old
        for thread_id, payload in entries:
            dates = payload.get("date_range", {})
            marker = f"<!-- terminal:{thread_id} -->"
            block = marker + "\n" + f"- [{dates.get('end') or dates.get('start') or '?'}]({payload.get('thread_link')}) — {payload.get('outcome') or 'completed'}"
            content = _bounded_digest_content(content, marker, block)
        retained = {
            str(thread_id) for thread_id, _ in entries
            if f"<!-- terminal:{thread_id} -->" in content
        }
        if not retained:
            raise ValueError("terminal digest batch retained no pending entries")
        response = self.client.update_message(
            digest.thread_id, digest.starter_message_id, content=content
        )
        self.client.update_thread(digest.thread_id, pinned=True)
        live_message = self.client.get_message(digest.thread_id, digest.starter_message_id)
        live_thread = self.client.get_channel(digest.thread_id)
        pinned = bool(live_thread.get("pinned") or (int(live_thread.get("flags") or 0) & 2))
        if str(live_message.get("content") or "") != content or not pinned:
            raise ValueError("batched digest content/pin not verified")
        return str(response.get("id") or digest.starter_message_id), retained

    def apply_done_tag(self, thread_id, payload, *, operation_key):
        forum = self.client.get_channel(self.cfg.forum_channel_id)
        lookup, _ = ensure_forum_tags(self.client, forum, ["done"])
        channel = self.client.get_channel(thread_id)
        workflow_ids = {
            str(tag.get("id"))
            for tag in forum.get("available_tags", [])
            if str(tag.get("name", "")).strip().lower() in _WORKFLOW_TAG_NAMES
        }
        tags = [
            str(tag_id) for tag_id in channel.get("applied_tags", [])
            if str(tag_id) not in workflow_ids
        ]
        tags.append(lookup["done"])
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
