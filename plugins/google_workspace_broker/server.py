from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import stat
import struct
import threading
from pathlib import Path

from .broker import Broker
from .calendar_state import CalendarOwnershipState
from .backend_google import GoogleWorkspaceBackend, MINIMUM_COMBINED_SCOPES
from . import protocol


def _validate_owned_directory(path: Path, *, uid: int, gid: int | None, label: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise PermissionError(f"{label} directory does not exist") from exc
    if stat.S_ISLNK(st.st_mode):
        raise PermissionError(f"{label} directory must not be a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"{label} path must be a directory")
    if st.st_uid != uid:
        raise PermissionError(f"{label} directory must be owned by expected uid")
    if gid is not None and st.st_gid != gid:
        raise PermissionError(f"{label} directory group must match expected gid")
    if stat.S_IMODE(st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(f"{label} directory must not be group/world writable")


def _open_validated_credentials(credentials_path: Path) -> int:
    if not credentials_path.is_absolute():
        raise PermissionError("credentials path must be absolute")
    _validate_owned_directory(credentials_path.parent, uid=os.geteuid(), gid=None, label="credentials parent")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(credentials_path, flags)
    except FileNotFoundError as exc:
        raise PermissionError("credentials file does not exist") from exc
    except OSError as exc:
        raise PermissionError("credentials file could not be opened securely") from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise PermissionError("credentials path must be a regular file")
    if st.st_uid != os.geteuid():
        os.close(fd)
        raise PermissionError("credentials file must be owned by broker uid")
    if stat.S_IMODE(st.st_mode) != 0o600:
        os.close(fd)
        raise PermissionError("credentials file mode must be exactly 0600")
    if not os.access(credentials_path, os.R_OK):
        os.close(fd)
        raise PermissionError("credentials file is not readable by broker")
    return fd


def _validate_socket_parent(socket_path: Path, socket_gid: int) -> None:
    if not socket_path.is_absolute():
        raise PermissionError("socket path must be absolute")
    _validate_owned_directory(socket_path.parent, uid=os.geteuid(), gid=socket_gid, label="socket parent")


def _peer_uid(conn: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    if hasattr(conn, "getpeereid"):
        uid, _gid = conn.getpeereid()
        return uid
    raise PermissionError("peer credentials are not available")


def _verify_client_uid(conn: socket.socket, expected_client_uid: int) -> None:
    if _peer_uid(conn) != expected_client_uid:
        raise PermissionError("client uid does not match configured Hermes uid")


def load_authorized_credentials(credentials_path: Path):
    """Load OAuth credentials inside the broker process only."""
    fd = _open_validated_credentials(credentials_path)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            info = json.load(fh)
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials.from_authorized_user_info(info, scopes=MINIMUM_COMBINED_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def serve(
    socket_path: Path,
    broker: Broker,
    *,
    socket_gid: int | None = None,
    client_uid: int | None = None,
    max_connections: int | None = None,
    allow_0600_unit_bypass: bool = False,
) -> None:
    if socket_gid is not None:
        _validate_socket_parent(socket_path, socket_gid)
        if client_uid is None:
            raise PermissionError("client uid is required for a 0660 broker socket")
        if client_uid == os.geteuid():
            raise PermissionError("client uid must differ from broker uid")
    elif client_uid is None and not allow_0600_unit_bypass:
        raise PermissionError("0600 broker socket mode requires an explicit unit-test bypass")
    elif client_uid == os.geteuid():
        raise PermissionError("client uid must differ from broker uid")
    if socket_path.exists() or socket_path.is_symlink():
        raise PermissionError("socket path already exists")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        if socket_gid is not None:
            os.chown(socket_path, os.geteuid(), socket_gid)
            os.chmod(socket_path, 0o660)
        else:
            os.chmod(socket_path, 0o600)
        mode = stat.S_IMODE(os.lstat(socket_path).st_mode)
        if mode not in (0o600, 0o660) or mode & 0o007:
            raise PermissionError("socket mode must be 0600 or 0660 with no world bits")
        server.listen(16)
        stop = False
        handled = 0

        def _stop(*_):
            nonlocal stop
            stop = True
            try:
                server.close()
            except OSError:
                pass

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)
        while not stop:
            try:
                conn, _ = server.accept()
            except OSError:
                break
            with conn:
                try:
                    if client_uid is not None:
                        _verify_client_uid(conn, client_uid)
                    data = protocol.read_to_eof(conn)
                    conn.sendall(broker.handle_wire(data))
                except PermissionError as exc:
                    conn.sendall(protocol.encode_response({"ok": False, "error": protocol.sanitize_error(exc)}))
                except protocol.ProtocolError as exc:
                    conn.sendall(protocol.encode_response({"ok": False, "error": protocol.sanitize_error(exc)}))
            handled += 1
            if max_connections is not None and handled >= max_connections:
                stop = True
    finally:
        try:
            server.close()
        finally:
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Google Workspace broker.")
    parser.add_argument("--socket", required=True, help="Absolute Unix socket path to create")
    parser.add_argument("--state", required=True, help="Broker-owned Calendar state JSON path")
    parser.add_argument("--credentials", required=True, help="OAuth authorized user JSON path")
    parser.add_argument("--socket-gid", type=int, help="Numeric shared group id for a 0660 broker socket")
    parser.add_argument("--client-uid", type=int, help="Numeric Hermes client uid allowed to connect")
    parser.add_argument("--print-plugin-config", action="store_true", help="Print non-secret plugin socket config JSON")
    args = parser.parse_args(argv)

    socket_path = Path(args.socket)
    if not socket_path.is_absolute():
        raise SystemExit("--socket must be absolute")
    if args.socket_gid is None:
        raise SystemExit("--socket-gid is required")
    if args.client_uid is None:
        raise SystemExit("--client-uid is required")
    if args.client_uid == os.geteuid():
        raise SystemExit("--client-uid must differ from broker uid")
    state_path = Path(args.state)
    credentials_path = Path(args.credentials)

    if args.print_plugin_config:
        print(json.dumps({
            "socket_path": str(socket_path),
            "expected_socket_uid": os.geteuid(),
            "expected_socket_gid": args.socket_gid if args.socket_gid is not None else os.getegid(),
        }, indent=2))
        return 0

    _validate_socket_parent(socket_path, args.socket_gid)
    credentials = load_authorized_credentials(credentials_path)
    broker = Broker(GoogleWorkspaceBackend(credentials), CalendarOwnershipState(state_path))
    serve(socket_path, broker, socket_gid=args.socket_gid, client_uid=args.client_uid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
