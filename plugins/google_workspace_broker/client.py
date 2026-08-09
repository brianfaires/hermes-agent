from __future__ import annotations

import json
import os
import socket
import stat
import struct
from pathlib import Path
from typing import Any

from . import protocol


class SocketConfigError(PermissionError):
    pass


_CONFIG_KEYS = {"socket_path", "expected_socket_uid", "expected_socket_gid"}


def _require_int(config: dict[str, Any], key: str) -> int:
    value = config.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SocketConfigError(f"google workspace broker {key} must be a numeric id")
    return value


class BrokerClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        expected_peer_uid: int | None = None,
        expected_peer_gid: int | None = None,
    ):
        self.socket_path = socket_path
        self.expected_peer_uid = expected_peer_uid
        self.expected_peer_gid = expected_peer_gid

    @classmethod
    def from_config_file(cls, config_path: str | Path) -> "BrokerClient":
        path = Path(config_path)
        if not path.is_absolute():
            raise SocketConfigError("google workspace broker config path must be absolute")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise SocketConfigError("google workspace broker config file does not exist") from exc
        except OSError as exc:
            raise SocketConfigError("google workspace broker config could not be opened securely") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise SocketConfigError("google workspace broker config must be a regular file")
            if st.st_uid != os.geteuid():
                raise SocketConfigError("google workspace broker config must be owned by current uid")
            if stat.S_IMODE(st.st_mode) & ~0o600:
                raise SocketConfigError("google workspace broker config mode must be 0600 or stricter")
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                fd = -1
                data = json.load(fh)
        except Exception as exc:
            if isinstance(exc, SocketConfigError):
                raise
            raise SocketConfigError("google workspace broker config is malformed") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(data, dict):
            raise SocketConfigError("google workspace broker config must be an object")
        return cls.from_config(data)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BrokerClient":
        if not isinstance(config, dict):
            raise SocketConfigError("google workspace broker config must be an object")
        if set(config) != _CONFIG_KEYS:
            raise SocketConfigError("google workspace broker config has invalid keys")
        raw = config.get("socket_path") if isinstance(config, dict) else None
        if not isinstance(raw, str) or not raw:
            raise SocketConfigError("google workspace broker socket_path is missing")
        expected_uid = _require_int(config, "expected_socket_uid")
        expected_gid = _require_int(config, "expected_socket_gid")
        if expected_uid == os.geteuid():
            raise SocketConfigError("google workspace broker must run under a separate uid")
        path = Path(raw)
        if not path.is_absolute():
            raise SocketConfigError("google workspace broker socket_path must be absolute")
        parent = path.parent
        try:
            parent_st = os.lstat(parent)
        except FileNotFoundError as exc:
            raise SocketConfigError("google workspace broker socket parent does not exist") from exc
        if stat.S_ISLNK(parent_st.st_mode):
            raise SocketConfigError("google workspace broker socket parent must not be a symlink")
        if not stat.S_ISDIR(parent_st.st_mode):
            raise SocketConfigError("google workspace broker socket parent must be a directory")
        if parent_st.st_uid != expected_uid or parent_st.st_gid != expected_gid:
            raise SocketConfigError("google workspace broker socket parent owner/group does not match config")
        if stat.S_IMODE(parent_st.st_mode) & 0o022:
            raise SocketConfigError("google workspace broker socket parent must not be group/world writable")
        try:
            st = os.lstat(path)
        except FileNotFoundError as exc:
            raise SocketConfigError("google workspace broker socket does not exist") from exc
        if stat.S_ISLNK(st.st_mode):
            raise SocketConfigError("google workspace broker socket must not be a symlink")
        if not stat.S_ISSOCK(st.st_mode):
            raise SocketConfigError("google workspace broker path is not a socket")
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o007:
            raise SocketConfigError("google workspace broker socket must not be world accessible")
        if mode != 0o660:
            raise SocketConfigError("google workspace broker socket mode must be exactly 0660")
        if st.st_uid != expected_uid:
            raise SocketConfigError("google workspace broker socket owner does not match config")
        if st.st_gid != expected_gid:
            raise SocketConfigError("google workspace broker socket group does not match config")
        if not os.access(path, os.R_OK | os.W_OK):
            raise SocketConfigError("google workspace broker socket is not accessible to current process")
        return cls(path, expected_peer_uid=expected_uid, expected_peer_gid=expected_gid)

    def _verify_peer_identity(self, sock: socket.socket) -> None:
        if self.expected_peer_uid is None or self.expected_peer_gid is None:
            return
        if hasattr(socket, "SO_PEERCRED"):
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, gid = struct.unpack("3i", creds)
        elif hasattr(sock, "getpeereid"):
            uid, gid = sock.getpeereid()
        else:
            raise SocketConfigError("google workspace broker peer identity is not available")
        if uid != self.expected_peer_uid or gid != self.expected_peer_gid:
            raise SocketConfigError("google workspace broker peer identity does not match config")

    def call(self, operation: str, params: dict[str, Any]) -> Any:
        data = protocol.encode_message(operation, params)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(str(self.socket_path))
            self._verify_peer_identity(sock)
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            response = protocol.read_to_eof(sock)
        payload = protocol.decode_response(response)
        if not payload["ok"]:
            raise RuntimeError(payload["error"])
        return payload.get("result")
