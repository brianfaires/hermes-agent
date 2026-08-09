from __future__ import annotations

import json
import re
import socket
from typing import Any

MAX_MESSAGE_BYTES = 64 * 1024
_SECRET_PATTERNS = [
    re.compile(r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._~+/\-]+=*"),
    re.compile(r"(?i)\b(?:access_token|refresh_token)\s*[=:]\s*[^,\s;&]+"),
    re.compile(r"ya29\.[A-Za-z0-9._-]+"),
    re.compile(r"1//[A-Za-z0-9._~+/\-]+"),
    re.compile(r"(?i)(secret|token|password|credential)[-_a-z0-9]*"),
]


class ProtocolError(ValueError):
    pass


def sanitize_error(exc: BaseException | str) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ")
    text = text.replace("```", "'''")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) > 300:
        text = text[:297] + "..."
    return text or "broker error"


def decode_message(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message too large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ProtocolError("malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("message must be an object")
    if set(payload) != {"operation", "params"}:
        raise ProtocolError("message must contain only operation and params")
    if not isinstance(payload["operation"], str) or not isinstance(payload["params"], dict):
        raise ProtocolError("invalid operation or params")
    return payload


def read_to_eof(sock: socket.socket) -> bytes:
    data = bytearray()
    while True:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message too large")
    return bytes(data)


def encode_message(operation: str, params: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        {"operation": operation, "params": params},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message too large")
    return encoded


def encode_response(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("response too large")
    return encoded


def decode_response(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("response too large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ProtocolError("malformed broker response") from exc
    if not isinstance(payload, dict) or "ok" not in payload:
        raise ProtocolError("invalid broker response")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise ProtocolError("invalid broker response")
    if ok:
        if set(payload) != {"ok", "result"}:
            raise ProtocolError("invalid broker response")
    elif set(payload) != {"ok", "error"} or not isinstance(payload.get("error"), str):
        raise ProtocolError("invalid broker response")
    return payload
