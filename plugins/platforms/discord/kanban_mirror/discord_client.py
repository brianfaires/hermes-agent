"""Discord HTTP client for the Kanban Discord Mirror.

Lifted from the Ops-profile reference implementation
(``~/.hermes/profiles/ops/scripts/kanban/discord_forum_mirror.py``) with three
deliberate changes for v2:

1. ``load_discord_token`` drops the v1 ops-profile/global ``.env`` fallback
   chain entirely — it only reads the given env file, then falls back to
   ``os.environ``.
2. ``update_thread`` gained a ``pinned: bool | None`` keyword so callers can
   pin/unpin a forum post via Discord's thread ``flags`` field (bit value 2 =
   ``PINNED``, i.e. ``1 << 1``) for the digest post.
3. ``update_thread``'s signature is now fully optional-kwargs (``name``,
   ``tag_ids``, ``archive``, ``pinned`` all default to ``None``): only keys
   for arguments that were actually passed are included in the PATCH payload.

This module is the only HTTP layer for the mirror; it never prints or logs
raw tokens (errors are redacted before being raised).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MESSAGE_LIMIT = 2000

# Bit 1 (value 2, i.e. ``1 << 1``) of a Discord thread's ``flags`` field is
# the PINNED flag for forum posts.
THREAD_FLAG_PINNED = 1 << 1

SECRET_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (
        re.compile(r"(?i)(bot\s+token|discord[_-]?token|api[_-]?key|secret|password|passwd|authorization)\s*[:=]\s*(?:(?:Bot|Bearer)\s+)?([^\s,;]+)"),
        lambda m: f"{m.group(1)}=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(Bot|Bearer)\s+[A-Za-z0-9._~+/-]{12,}"),
        lambda m: f"{m.group(1)} [REDACTED]",
    ),
    (
        re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b"),
        lambda m: "[REDACTED_DISCORD_TOKEN]",
    ),
]


def sanitize_error(value: Any) -> str:
    """Redact secret-shaped substrings and truncate before surfacing errors."""
    text = "" if value is None else str(value)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    max_chars = 800
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + f"\n… [truncated {len(text) - (max_chars - 40)} chars]"
    return text


@dataclass
class DiscordResponse:
    status: int
    data: Any
    text: str


class DiscordAPIError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        self.method = method
        self.path = path
        self.status = status
        self.body = sanitize_error(body)
        super().__init__(f"Discord API {method} {path} failed with HTTP {status}: {self.body}")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def load_discord_token(env_path: Path, *, allow_process_fallback: bool = True) -> str:
    """Load the live Discord token from ``env_path``.

    Legacy single-profile/CLI callers may fall back to ``os.environ``. A
    multiplex-owned runtime passes ``allow_process_fallback=False`` so a
    secondary owner can never inherit the default profile's process token.
    """
    values = parse_env_file(env_path)
    token = values.get("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    if allow_process_fallback:
        return os.getenv("DISCORD_BOT_TOKEN", "").strip()
    return ""


def _multipart_payload(payload: dict[str, Any], attachments: list[str]) -> tuple[bytes, str]:
    boundary = f"HermesKanbanMirror{uuid.uuid4().hex}"
    crlf = b"\r\n"
    body = bytearray()

    def add_part(headers: list[tuple[str, str]], content: bytes) -> None:
        body.extend(f"--{boundary}".encode("utf-8"))
        body.extend(crlf)
        for key, value in headers:
            body.extend(f"{key}: {value}".encode("utf-8"))
            body.extend(crlf)
        body.extend(crlf)
        body.extend(content)
        body.extend(crlf)

    add_part(
        [("Content-Disposition", 'form-data; name="payload_json"'), ("Content-Type", "application/json")],
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    for idx, raw_path in enumerate(attachments):
        file_path = Path(raw_path)
        data = file_path.read_bytes()
        mime, _ = mimetypes.guess_type(file_path.name)
        add_part(
            [
                ("Content-Disposition", f'form-data; name="files[{idx}]"; filename="{file_path.name}"'),
                ("Content-Type", mime or "application/octet-stream"),
            ],
            data,
        )
    body.extend(f"--{boundary}--".encode("utf-8"))
    body.extend(crlf)
    return bytes(body), boundary


class DiscordClient:
    def __init__(self, token: str, *, timeout: int = 30):
        if not token:
            raise SystemExit("DISCORD_BOT_TOKEN is not configured")
        self.token = token
        self.timeout = timeout

    _MAX_429_RETRIES = 3
    _MAX_TRANSIENT_RETRIES = 3
    _TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504})

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: set[int] = {200},
        files: list[str] | None = None,
    ) -> DiscordResponse:
        url = f"{DISCORD_API_BASE}{path}"
        data = None
        headers = {"Authorization": f"Bot {self.token}", "User-Agent": "Hermes-Kanban-Discord-Mirror/2.0"}
        if files:
            data, boundary = _multipart_payload(payload or {}, files)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        rate_limit_attempt = 0
        transient_attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                    parsed = json.loads(text) if text else None
                    return DiscordResponse(resp.status, parsed, text)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code == 429:
                    rate_limit_attempt += 1
                    if rate_limit_attempt > self._MAX_429_RETRIES:
                        raise DiscordAPIError(method, path, e.code, body)
                    try:
                        retry_after = float((json.loads(body) or {}).get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    time.sleep(min(max(retry_after, 0.5), 10.0))
                    continue
                if method == "GET" and e.code in self._TRANSIENT_HTTP_STATUSES:
                    transient_attempt += 1
                    if transient_attempt < self._MAX_TRANSIENT_RETRIES:
                        time.sleep(0.5 * (2 ** (transient_attempt - 1)))
                        continue
                if e.code not in expected:
                    raise DiscordAPIError(method, path, e.code, body)
                parsed = json.loads(body) if body else None
                return DiscordResponse(e.code, parsed, body)
            except urllib.error.URLError as e:
                if method == "GET":
                    transient_attempt += 1
                    if transient_attempt < self._MAX_TRANSIENT_RETRIES:
                        time.sleep(0.5 * (2 ** (transient_attempt - 1)))
                        continue
                raise RuntimeError(f"Discord API network error for {method} {path}: {sanitize_error(e)}") from e

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        resp = self.request("GET", f"/channels/{channel_id}", expected={200})
        return resp.data or {}

    def get_current_user(self) -> dict[str, Any]:
        resp = self.request("GET", "/users/@me", expected={200})
        return resp.data or {}

    def list_active_threads(self, guild_id: str) -> list[dict[str, Any]]:
        resp = self.request("GET", f"/guilds/{guild_id}/threads/active", expected={200})
        data = resp.data or {}
        threads = data.get("threads") if isinstance(data, dict) else []
        if not isinstance(threads, list):
            return []
        return [thread for thread in threads if isinstance(thread, dict)]

    def get_message(self, channel_id: str, message_id: str) -> dict[str, Any]:
        resp = self.request("GET", f"/channels/{channel_id}/messages/{message_id}", expected={200})
        return resp.data or {}

    def update_forum_tags(self, channel_id: str, available_tags: list[dict[str, Any]]) -> dict[str, Any]:
        resp = self.request("PATCH", f"/channels/{channel_id}", {"available_tags": available_tags}, expected={200})
        return resp.data or {}

    def create_forum_thread(
        self,
        forum_channel_id: str,
        *,
        name: str,
        content: str,
        tag_ids: list[str],
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"content": content, "allowed_mentions": {"parse": []}}
        files = [p for p in (attachments or []) if Path(p).is_file()]
        if files:
            message["attachments"] = [{"id": idx, "filename": Path(path).name} for idx, path in enumerate(files)]
        payload: dict[str, Any] = {"name": name, "message": message}
        if tag_ids:
            payload["applied_tags"] = tag_ids
        resp = self.request("POST", f"/channels/{forum_channel_id}/threads", payload, expected={200, 201}, files=files or None)
        return resp.data or {}

    def update_thread(
        self,
        thread_id: str,
        *,
        name: str | None = None,
        tag_ids: list[str] | None = None,
        archive: bool | None = None,
        pinned: bool | None = None,
        locked: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if tag_ids is not None:
            payload["applied_tags"] = tag_ids
        if archive is not None:
            payload["archived"] = bool(archive)
        if pinned is not None:
            # Discord thread ``flags`` bit 1 (value 2, ``1 << 1``) is PINNED.
            payload["flags"] = THREAD_FLAG_PINNED if pinned else 0
        if locked is not None:
            payload["locked"] = bool(locked)
        resp = self.request("PATCH", f"/channels/{thread_id}", payload, expected={200})
        return resp.data or {}

    def update_message(self, channel_id: str, message_id: str, *, content: str) -> dict[str, Any]:
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        resp = self.request("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload, expected={200})
        return resp.data or {}

    def create_dm(self, user_id: str) -> dict[str, Any]:
        resp = self.request("POST", "/users/@me/channels", {"recipient_id": str(user_id)}, expected={200})
        return resp.data or {}

    def send_message(self, channel_id: str, *, content: str, nonce: str | None = None) -> dict[str, Any]:
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        if nonce is not None:
            payload.update({"nonce": nonce, "enforce_nonce": True})
        resp = self.request("POST", f"/channels/{channel_id}/messages", payload, expected={200, 201})
        return resp.data or {}


def available_tag_ids(channel: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in channel.get("available_tags") or []:
        name = str(tag.get("name") or "").strip().lower()
        tag_id = str(tag.get("id") or "").strip()
        if name and tag_id:
            out[name] = tag_id
    return out


def ensure_forum_tags(client: DiscordClient, channel: dict[str, Any], wanted_names: list[str]) -> tuple[dict[str, str], list[str]]:
    """Create missing Forum tags by PATCHing available_tags, preserving existing tags."""
    existing = list(channel.get("available_tags") or [])
    lookup = available_tag_ids(channel)
    created: list[str] = []
    for raw_name in wanted_names:
        name = raw_name.strip().lower()
        if not name or name in lookup:
            continue
        existing.append({"name": name, "moderated": False})
        lookup[name] = "pending"
        created.append(name)
    if not created:
        return available_tag_ids(channel), []
    updated = client.update_forum_tags(str(channel["id"]), existing)
    return available_tag_ids(updated), created


def split_discord_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit - 20)
        if cut < 200:
            cut = limit - 20
        chunks.append(remaining[:cut].rstrip() + "\n…")
        remaining = "…\n" + remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
