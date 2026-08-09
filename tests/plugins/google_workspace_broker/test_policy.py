import json
import os
import socket
import stat
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from plugins.google_workspace_broker import plugin as gw_plugin
from plugins.google_workspace_broker import protocol
from plugins.google_workspace_broker import server as gw_server
from plugins.google_workspace_broker.broker import Broker, PolicyError
from plugins.google_workspace_broker.calendar_state import CalendarOwnershipState
from plugins.google_workspace_broker.client import BrokerClient, SocketConfigError
from plugins.google_workspace_broker.backup import create_backup
from plugins.google_workspace_broker.server import load_authorized_credentials, serve
from model_tools import get_tool_definitions, handle_function_call
from tools import registry


class FakeBackend:
    def __init__(self):
        self.calls = []

    def list_calendars(self, params):
        self.calls.append(("list_calendars", params))
        return {"items": []}

    def list_events(self, params):
        self.calls.append(("list_events", params))
        return {"items": []}

    def get_event(self, params):
        self.calls.append(("get_event", params))
        return {"id": params["event_id"]}

    def create_calendar(self, params):
        self.calls.append(("create_calendar", params))
        return {"id": "cal-created", "summary": params["summary"]}

    def create_event(self, params):
        self.calls.append(("create_event", params))
        return {"id": "event-created"}

    def update_event(self, params):
        self.calls.append(("update_event", params))
        return {"id": params["event_id"]}

    def search_messages(self, params):
        self.calls.append(("search_messages", params))
        return {"messages": []}

    def get_message(self, params):
        self.calls.append(("get_message", params))
        return {"id": params["message_id"]}

    def get_thread(self, params):
        self.calls.append(("get_thread", params))
        return {"id": params["thread_id"]}

    def list_labels(self, params):
        self.calls.append(("list_labels", params))
        return {"labels": []}

    def create_label(self, params):
        self.calls.append(("create_label", params))
        return {"id": "Label_1"}

    def update_label(self, params):
        self.calls.append(("update_label", params))
        return {"id": params["label_id"]}

    def delete_label(self, params):
        self.calls.append(("delete_label", params))
        return {"deleted": params["label_id"]}

    def modify_message_labels(self, params):
        self.calls.append(("modify_message_labels", params))
        return {"id": params["message_id"]}

    def create_draft(self, params):
        self.calls.append(("create_draft", params))
        return {"id": "draft-1"}


def write_state(path: Path, ids=None):
    path.write_text(json.dumps({"calendar_ids": ids or []}), encoding="utf-8")
    path.chmod(0o600)


def secure_config(tmp_path: Path, sock_path: Path, *, uid: int | None = None, gid: int | None = None) -> Path:
    cfg = tmp_path / "broker-config.json"
    cfg.write_text(json.dumps({
        "socket_path": str(sock_path),
        "expected_socket_uid": os.geteuid() if uid is None else uid,
        "expected_socket_gid": os.getegid() if gid is None else gid,
    }), encoding="utf-8")
    cfg.chmod(0o600)
    return cfg


def test_plugin_registers_exact_toolsets_with_closed_schemas():
    calls = []

    class Ctx:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

    gw_plugin.register(Ctx())

    assert {c["name"] for c in calls} == {
        "google_calendar_read",
        "google_calendar_manage",
        "google_gmail_read",
        "google_gmail_labels",
        "google_gmail_drafts",
    }
    assert {c["toolset"] for c in calls} == {c["name"] for c in calls}

    forbidden = {"action", "endpoint", "url", "path", "body", "method", "token"}
    for call in calls:
        schema = call["schema"]
        assert schema["parameters"]["additionalProperties"] is False
        dumped = json.dumps(schema)
        assert not (forbidden & set(schema["parameters"]["properties"]))
        assert "additionalProperties" in dumped


def test_plugin_registers_with_real_plugin_context_signature(monkeypatch):
    calls = []

    def fake_registry_register(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("tools.registry.registry.register", fake_registry_register)
    manager = PluginManager()
    ctx = PluginContext(PluginManifest(name="google-workspace-broker"), manager)

    gw_plugin.register(ctx)

    assert {c["name"] for c in calls} == {
        "google_calendar_read",
        "google_calendar_manage",
        "google_gmail_read",
        "google_gmail_labels",
        "google_gmail_drafts",
    }
    assert manager._plugin_tool_names == {c["name"] for c in calls}
    assert all("cache_check_fn" not in c for c in calls)


def test_plugin_handlers_reject_forged_cross_tool_operations_before_socket():
    calls = {}

    class Ctx:
        def register_tool(self, **kwargs):
            calls[kwargs["name"]] = kwargs

    gw_plugin.register(Ctx())

    result = json.loads(calls["google_calendar_read"]["handler"]({
        "operation": "calendar.create_calendar",
        "summary": "forged",
    }))
    assert "error" in result
    assert "not available" in result["error"]

    result = json.loads(calls["google_gmail_read"]["handler"]({
        "operation": "gmail.create_draft",
        "to": ["a@example.com"],
        "subject": "x",
        "body_text": "x",
    }))
    assert "error" in result
    assert "not available" in result["error"]


def test_broker_rejects_unknown_operation_and_unexpected_params_before_backend(tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path, ["owned"])
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))

    with pytest.raises(PolicyError):
        broker.dispatch({"operation": "send_mail", "params": {}})
    with pytest.raises(PolicyError):
        broker.dispatch({"operation": ["gmail.search_messages"], "params": {}})
    with pytest.raises(PolicyError):
        broker.dispatch({"operation": "gmail.search_messages", "params": {"query": "x", "endpoint": "/gmail/v1/users/me/messages"}})

    assert backend.calls == []


def test_calendar_manage_protected_targets_never_reach_backend(tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path, ["owned-cal"])
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))

    with pytest.raises(PolicyError):
        broker.dispatch({
            "operation": "calendar.create_event",
            "params": {"calendar_id": "primary", "summary": "x", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}},
        })
    assert backend.calls == []

    out = broker.dispatch({
        "operation": "calendar.create_event",
        "params": {"calendar_id": "owned-cal", "summary": "x", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}},
    })
    assert out["id"] == "event-created"
    assert backend.calls[-1][0] == "create_event"


def test_calendar_write_rejects_nested_or_malformed_fields_before_backend(tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path, ["owned-cal"])
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))

    bad_params = [
        {"calendar_id": "owned-cal", "summary": "x", "start": {"date": "2026-01-01", "raw": "x"}, "end": {"date": "2026-01-02"}},
        {"calendar_id": "owned-cal", "summary": "x", "start": {"timeZone": "UTC"}, "end": {"date": "2026-01-02"}},
        {"calendar_id": "owned-cal", "summary": "x", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}, "attendee_emails": ["ok@example.com", ""]},
        {"calendar_id": "owned-cal", "summary": "x", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}, "description": 1},
    ]
    for params in bad_params:
        with pytest.raises(PolicyError):
            broker.dispatch({"operation": "calendar.create_event", "params": params})

    assert backend.calls == []


def test_calendar_state_fail_closed_on_missing_symlink_insecure_and_unwritable(tmp_path):
    for bad in [tmp_path / "missing.json"]:
        with pytest.raises(PolicyError):
            CalendarOwnershipState(bad).contains("x")

    real = tmp_path / "real.json"
    write_state(real, ["x"])
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(PolicyError):
        CalendarOwnershipState(link).contains("x")

    insecure = tmp_path / "insecure.json"
    write_state(insecure, ["x"])
    insecure.chmod(0o644)
    with pytest.raises(PolicyError):
        CalendarOwnershipState(insecure).contains("x")

    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(PolicyError):
        CalendarOwnershipState(directory).contains("x")


def test_calendar_create_tracks_only_after_successful_backend_create(tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path)
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))

    broker.dispatch({"operation": "calendar.create_calendar", "params": {"summary": "Owned"}})
    assert CalendarOwnershipState(state_path).contains("cal-created")

    class FailingBackend(FakeBackend):
        def create_calendar(self, params):
            raise RuntimeError("secret-token should not leak")

    state2 = tmp_path / "state2.json"
    write_state(state2)
    with pytest.raises(RuntimeError):
        Broker(FailingBackend(), CalendarOwnershipState(state2)).dispatch({"operation": "calendar.create_calendar", "params": {"summary": "Nope"}})
    assert json.loads(state2.read_text(encoding="utf-8"))["calendar_ids"] == []


@pytest.mark.parametrize("operation,params", [
    ("gmail.delete_label", {"label_id": "INBOX"}),
    ("gmail.create_label", {"name": "INBOX"}),
    ("gmail.update_label", {"label_id": "Label_1", "name": "TRASH"}),
    ("gmail.update_label", {"label_id": "CATEGORY_UPDATES", "name": "x"}),
    ("gmail.modify_message_labels", {"message_id": "m1", "add_label_ids": ["Label_1", "TRASH"], "remove_label_ids": []}),
    ("gmail.modify_message_labels", {"message_id": "m1", "add_label_ids": ["bad/id"], "remove_label_ids": []}),
    ("gmail.create_draft", {"to": ["a@example.com"], "subject": "s", "body_text": "b", "send": True}),
    ("gmail.trash_message", {"message_id": "m1"}),
])
def test_gmail_forbidden_paths_and_system_label_abuse(operation, params, tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path)
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))

    with pytest.raises(PolicyError):
        broker.dispatch({"operation": operation, "params": params})
    assert backend.calls == []


def test_protocol_rejects_malformed_oversized_and_sanitizes_errors(tmp_path):
    assert "secret" not in protocol.sanitize_error(RuntimeError("secret-token\n```")).lower()
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_message(b"{not-json\n")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_message(b"x" * (protocol.MAX_MESSAGE_BYTES + 1))


def test_invalid_missing_socket_config_fails_closed(tmp_path):
    with pytest.raises(SocketConfigError):
        BrokerClient.from_config({})
    with pytest.raises(SocketConfigError):
        BrokerClient.from_config({"socket_path": "relative.sock"})

    sock = tmp_path / "broker.sock"
    sock.write_text("", encoding="utf-8")
    sock.chmod(0o666)
    with pytest.raises(SocketConfigError):
        BrokerClient.from_config({"socket_path": str(sock)})


def test_plugin_config_file_rejects_symlink_extra_keys_wrong_owner_and_bad_modes(tmp_path, monkeypatch):
    sock = tmp_path / "broker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    sock.chmod(0o600)
    try:
        good = secure_config(tmp_path, sock)
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config_file(good)

        extra = tmp_path / "extra.json"
        extra.write_text(json.dumps({
            "socket_path": str(sock),
            "expected_socket_uid": os.geteuid(),
            "expected_socket_gid": os.getegid(),
            "debug": True,
        }), encoding="utf-8")
        extra.chmod(0o600)
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config_file(extra)

        loose = tmp_path / "loose.json"
        loose.write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
        loose.chmod(0o640)
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config_file(loose)

        link = tmp_path / "link.json"
        link.symlink_to(good)
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config_file(link)

        real_fstat = os.fstat

        def fake_fstat(fd):
            st = real_fstat(fd)
            fd_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if fd_path == good:
                values = list(st)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return st

        monkeypatch.setattr(os, "fstat", fake_fstat)
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config_file(good)
    finally:
        server.close()


def test_plugin_config_file_is_loaded_from_open_fd_not_path_open(tmp_path, monkeypatch):
    cfg = tmp_path / "broker-config.json"
    cfg.write_text(json.dumps({
        "socket_path": str(tmp_path / "missing.sock"),
        "expected_socket_uid": os.geteuid() + 1,
        "expected_socket_gid": os.getegid(),
    }), encoding="utf-8")
    cfg.chmod(0o600)

    def path_open_must_not_run(*args, **kwargs):
        raise AssertionError("config loader must not reopen with Path.open")

    monkeypatch.setattr(Path, "open", path_open_must_not_run)
    with pytest.raises(SocketConfigError, match="socket parent"):
        BrokerClient.from_config_file(cfg)


def test_socket_config_rejects_same_uid_and_0600_production_socket(tmp_path, monkeypatch):
    sock = tmp_path / "broker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    try:
        cfg = {
            "socket_path": str(sock),
            "expected_socket_uid": os.geteuid(),
            "expected_socket_gid": os.getegid(),
        }
        sock.chmod(0o660)
        with pytest.raises(SocketConfigError, match="separate uid"):
            BrokerClient.from_config(cfg)

        monkeypatch.setattr(os, "geteuid", lambda: cfg["expected_socket_uid"] + 1)
        sock.chmod(0o600)
        with pytest.raises(SocketConfigError, match="mode must be exactly 0660"):
            BrokerClient.from_config(cfg)

        sock.chmod(0o660)
        assert BrokerClient.from_config(cfg).socket_path == sock
    finally:
        server.close()


def test_client_binds_connected_peer_identity():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        BrokerClient(Path("/unused"), expected_peer_uid=os.geteuid(), expected_peer_gid=os.getegid())._verify_peer_identity(left)
        with pytest.raises(SocketConfigError, match="peer identity"):
            BrokerClient(
                Path("/unused"),
                expected_peer_uid=os.geteuid() + 1,
                expected_peer_gid=os.getegid(),
            )._verify_peer_identity(left)
    finally:
        left.close()
        right.close()


def test_socket_config_validates_strict_parent_identity_and_permissions(tmp_path, monkeypatch):
    sock = tmp_path / "broker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    sock.chmod(0o660)
    cfg = {
        "socket_path": str(sock),
        "expected_socket_uid": os.geteuid(),
        "expected_socket_gid": os.getegid(),
    }
    monkeypatch.setattr(os, "geteuid", lambda: cfg["expected_socket_uid"] + 1)
    real_lstat = os.lstat

    def wrong_parent_lstat(path):
        st = real_lstat(path)
        if Path(path) == tmp_path:
            values = list(st)
            values[4] = cfg["expected_socket_uid"] + 99
            return os.stat_result(values)
        return st

    try:
        monkeypatch.setattr(os, "lstat", wrong_parent_lstat)
        with pytest.raises(SocketConfigError, match="parent"):
            BrokerClient.from_config(cfg)
        monkeypatch.setattr(os, "lstat", real_lstat)
        tmp_path.chmod(0o770)
        with pytest.raises(SocketConfigError, match="parent"):
            BrokerClient.from_config(cfg)
    finally:
        tmp_path.chmod(0o700)
        server.close()


def test_socket_config_requires_expected_uid_gid_and_rejects_world_bits(tmp_path):
    sock = tmp_path / "broker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    sock.chmod(0o660)
    try:
        cfg = {
            "socket_path": str(sock),
            "expected_socket_uid": os.geteuid(),
            "expected_socket_gid": os.getegid(),
        }
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config({**cfg, "expected_socket_uid": os.geteuid() + 1})
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config({**cfg, "expected_socket_gid": os.getegid() + 1})

        sock.chmod(0o662)
        with pytest.raises(SocketConfigError):
            BrokerClient.from_config(cfg)
    finally:
        server.close()


def test_fake_socket_morning_brief_uses_only_read_toolsets(tmp_path):
    sock_path = tmp_path / "broker.sock"
    state_path = tmp_path / "state.json"
    write_state(state_path)
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    sock_path.chmod(0o600)
    server.listen(1)

    def serve_two():
        for _ in range(2):
            conn, _ = server.accept()
            with conn:
                data = conn.recv(protocol.MAX_MESSAGE_BYTES + 1)
                response = broker.handle_wire(data)
                conn.sendall(response)

    thread = threading.Thread(target=serve_two)
    thread.start()
    client = BrokerClient(sock_path)

    cal = client.call("calendar.list_events", {"calendar_id": "primary"})
    mail = client.call("gmail.search_messages", {"query": "newer_than:1d"})

    thread.join(timeout=3)
    server.close()
    assert cal == {"items": []}
    assert mail == {"messages": []}
    assert [name for name, _ in backend.calls] == ["list_events", "search_messages"]


def test_morning_brief_toolsets_are_isolated_through_registry_and_handlers(tmp_path, monkeypatch):
    tool_names = {
        "google_calendar_read",
        "google_calendar_manage",
        "google_gmail_read",
        "google_gmail_labels",
        "google_gmail_drafts",
    }
    for name in tool_names:
        registry.registry.deregister(name)

    class RegistryCtx:
        def register_tool(self, **kwargs):
            registry.registry.register(**kwargs)

    sock_path = tmp_path / "broker.sock"
    state_path = tmp_path / "state.json"
    write_state(state_path)
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    sock_path.chmod(0o600)
    server.listen(2)

    def serve_two():
        for _ in range(2):
            conn, _ = server.accept()
            with conn:
                data = protocol.read_to_eof(conn)
                conn.sendall(broker.handle_wire(data))
        server.close()

    cfg = secure_config(tmp_path, sock_path)
    monkeypatch.setenv("GOOGLE_WORKSPACE_BROKER_CONFIG", str(cfg))
    monkeypatch.setattr(gw_plugin.BrokerClient, "from_config_file", lambda _path: BrokerClient(sock_path))
    thread = threading.Thread(target=serve_two, daemon=True)
    thread.start()
    try:
        gw_plugin.register(RegistryCtx())
        defs = get_tool_definitions(
            enabled_toolsets=["google_calendar_read", "google_gmail_read"],
            disabled_toolsets=[],
            quiet_mode=True,
        )
        names = {td["function"]["name"] for td in defs}
        assert names == {"google_calendar_read", "google_gmail_read"}
        assert not (names & {
            "google_calendar_manage",
            "google_gmail_labels",
            "google_gmail_drafts",
            "execute_code",
            "read_terminal",
            "close_terminal",
        })

        cal = json.loads(handle_function_call(
            "google_calendar_read",
            {"operation": "calendar.list_events", "calendar_id": "primary"},
            enabled_tools=list(names),
            enabled_toolsets=["google_calendar_read", "google_gmail_read"],
        ))
        mail = json.loads(handle_function_call(
            "google_gmail_read",
            {"operation": "gmail.search_messages", "query": "newer_than:1d"},
            enabled_tools=list(names),
            enabled_toolsets=["google_calendar_read", "google_gmail_read"],
        ))
        assert cal == {"items": []}
        assert mail == {"messages": []}
        assert [name for name, _ in backend.calls] == ["list_events", "search_messages"]
    finally:
        try:
            server.close()
        except OSError:
            pass
        thread.join(timeout=3)
        for name in tool_names:
            registry.registry.deregister(name)


def test_backup_helper_requires_gate_and_never_copies_secret_contents(tmp_path):
    src = tmp_path / "token.json"
    src.write_text("super-secret-token", encoding="utf-8")
    src.chmod(0o600)
    out_dir = tmp_path / "backup"

    with pytest.raises(PermissionError):
        create_backup([src], out_dir, approved=False)

    manifest_path = create_backup([src], out_dir, approved=True)
    mode = stat.S_IMODE(out_dir.stat().st_mode)
    assert mode == 0o700
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["sources"][0]["path"] == str(src)
    assert "source_sha256" in manifest["sources"][0]
    assert "super-secret-token" not in manifest_path.read_text(encoding="utf-8")
    copy_path = out_dir / "files" / manifest["sources"][0]["backup_name"]
    assert stat.S_IMODE(copy_path.stat().st_mode) == 0o600
    assert copy_path.read_text(encoding="utf-8") == "super-secret-token"


def test_backup_rejects_symlink_non_file_existing_destination_and_cleans_partial(tmp_path, capsys):
    src = tmp_path / "token.json"
    src.write_bytes(b"secret bytes")
    src.chmod(0o644)
    link = tmp_path / "link.json"
    link.symlink_to(src)
    with pytest.raises(ValueError):
        create_backup([link], tmp_path / "backup-link", approved=True)
    with pytest.raises(ValueError):
        create_backup([tmp_path], tmp_path / "backup-dir", approved=True)

    out = tmp_path / "backup"
    create_backup([src], out, approved=True)
    with pytest.raises(FileExistsError):
        create_backup([src], out, approved=True)

    bad = tmp_path / "bad"
    bad.write_bytes(b"x")
    bad.chmod(0o600)
    fail_out = tmp_path / "backup-fail"
    with pytest.raises(ValueError):
        create_backup([src, tmp_path], fail_out, approved=True)
    assert not fail_out.exists()
    assert "secret bytes" not in capsys.readouterr().out


def test_backup_opens_each_source_once_and_manifest_uses_open_fd_metadata(tmp_path, monkeypatch):
    src = tmp_path / "token.json"
    content = b"race-resistant-secret"
    src.write_bytes(content)
    src.chmod(0o600)
    real_open = os.open
    real_lstat = os.lstat
    source_opens = 0

    def counting_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal source_opens
        if Path(path) == src:
            source_opens += 1
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def misleading_lstat(path):
        st = real_lstat(path)
        if Path(path) == src:
            values = list(st)
            values[6] = st.st_size + 100
            return os.stat_result(values)
        return st

    monkeypatch.setattr(os, "open", counting_open)
    monkeypatch.setattr(os, "lstat", misleading_lstat)
    manifest_path = create_backup([src], tmp_path / "backup-race", approved=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert source_opens == 1
    entry = manifest["sources"][0]
    assert entry["size"] == len(content)
    assert entry["source_sha256"] == entry["copy_sha256"]


def test_server_and_client_use_eof_framing_for_fragmented_messages(tmp_path):
    state = tmp_path / "state.json"
    write_state(state)
    sock_path = tmp_path / "broker.sock"
    broker = Broker(FakeBackend(), CalendarOwnershipState(state))
    ready = threading.Event()

    def run_server():
        ready.set()
        serve(sock_path, broker, max_connections=1, allow_0600_unit_bypass=True)

    thread = threading.Thread(target=run_server)
    thread.start()
    ready.wait(1)
    for _ in range(100):
        if sock_path.exists():
            break
        time.sleep(0.01)
    req = protocol.encode_message("gmail.search_messages", {"query": "x"})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(sock_path))
        for idx in range(0, len(req), 3):
            sock.sendall(req[idx:idx + 3])
        sock.shutdown(socket.SHUT_WR)
        data = bytearray()
        while True:
            chunk = sock.recv(2)
            if not chunk:
                break
            data.extend(chunk)
    thread.join(timeout=3)
    payload = json.loads(bytes(data).decode("utf-8"))
    assert payload["ok"] is True
    assert payload["result"] == {"messages": []}


def test_server_cli_requires_socket_gid_client_uid_and_validates_socket_parent(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    write_state(state)
    cred = tmp_path / "cred.json"
    cred.write_text("{}", encoding="utf-8")
    cred.chmod(0o600)
    sock = tmp_path / "broker.sock"

    with pytest.raises(SystemExit, match="--socket-gid"):
        gw_server.main([
            "--socket", str(sock),
            "--state", str(state),
            "--credentials", str(cred),
        ])

    with pytest.raises(SystemExit, match="--client-uid"):
        gw_server.main([
            "--socket", str(sock),
            "--socket-gid", str(os.getegid()),
            "--state", str(state),
            "--credentials", str(cred),
        ])

    with pytest.raises(SystemExit, match="--client-uid"):
        gw_server.main([
            "--socket", str(sock),
            "--socket-gid", str(os.getegid()),
            "--client-uid", str(os.geteuid()),
            "--state", str(state),
            "--credentials", str(cred),
        ])

    monkeypatch.setattr(gw_server, "load_authorized_credentials", lambda _path: object())
    monkeypatch.setattr(gw_server, "GoogleWorkspaceBackend", lambda _credentials: FakeBackend())
    called = {}

    def fake_serve(socket_path, broker, *, socket_gid=None, client_uid=None, max_connections=None):
        called["args"] = (socket_path, socket_gid, client_uid)

    monkeypatch.setattr(gw_server, "serve", fake_serve)
    tmp_path.chmod(0o770)
    with pytest.raises(PermissionError, match="socket parent"):
        gw_server.main([
            "--socket", str(sock),
            "--socket-gid", str(os.getegid()),
            "--client-uid", str(os.geteuid() + 1),
            "--state", str(state),
            "--credentials", str(cred),
        ])
    assert called == {}
    tmp_path.chmod(0o700)

    assert gw_server.main([
        "--socket", str(sock),
        "--socket-gid", str(os.getegid()),
        "--client-uid", str(os.geteuid() + 1),
        "--state", str(state),
        "--credentials", str(cred),
    ]) == 0
    assert called["args"] == (sock, os.getegid(), os.geteuid() + 1)


def test_server_requires_explicit_0600_unit_bypass_or_client_uid(tmp_path):
    state = tmp_path / "state.json"
    write_state(state)
    broker = Broker(FakeBackend(), CalendarOwnershipState(state))

    with pytest.raises(PermissionError, match="unit-test bypass"):
        serve(tmp_path / "broker.sock", broker, max_connections=1)

    with pytest.raises(PermissionError, match="client uid"):
        serve(tmp_path / "broker-0660.sock", broker, socket_gid=os.getegid(), max_connections=1)

    with pytest.raises(PermissionError, match="client uid"):
        serve(
            tmp_path / "broker-same-uid.sock",
            broker,
            socket_gid=os.getegid(),
            client_uid=os.geteuid(),
            max_connections=1,
        )


def test_server_0660_accepts_matching_client_uid_before_dispatch(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    write_state(state)
    sock_path = tmp_path / "broker.sock"
    expected_client_uid = os.geteuid() + 1
    broker = Broker(FakeBackend(), CalendarOwnershipState(state))
    monkeypatch.setattr(gw_server, "_peer_uid", lambda _conn: expected_client_uid)
    ready = threading.Event()

    def run_server():
        ready.set()
        serve(
            sock_path,
            broker,
            socket_gid=os.getegid(),
            client_uid=expected_client_uid,
            max_connections=1,
        )

    thread = threading.Thread(target=run_server)
    thread.start()
    ready.wait(1)
    for _ in range(100):
        if sock_path.exists():
            break
        time.sleep(0.01)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(sock_path))
        sock.sendall(protocol.encode_message("gmail.search_messages", {"query": "x"}))
        sock.shutdown(socket.SHUT_WR)
        response = protocol.read_to_eof(sock)
    thread.join(timeout=3)
    assert protocol.decode_response(response) == {"ok": True, "result": {"messages": []}}


def test_server_0660_rejects_mismatched_client_uid_before_handle_wire(tmp_path, monkeypatch):
    class TrackingBroker:
        called = False

        def handle_wire(self, data):
            self.called = True
            raise AssertionError("handle_wire must not run for mismatched client uid")

    sock_path = tmp_path / "broker.sock"
    expected_client_uid = os.geteuid() + 1
    broker = TrackingBroker()
    monkeypatch.setattr(gw_server, "_peer_uid", lambda _conn: expected_client_uid + 1)
    ready = threading.Event()

    def run_server():
        ready.set()
        serve(
            sock_path,
            broker,
            socket_gid=os.getegid(),
            client_uid=expected_client_uid,
            max_connections=1,
        )

    thread = threading.Thread(target=run_server)
    thread.start()
    ready.wait(1)
    for _ in range(100):
        if sock_path.exists():
            break
        time.sleep(0.01)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(sock_path))
        response = protocol.read_to_eof(sock)
    thread.join(timeout=3)
    payload = protocol.decode_response(response)
    assert payload["ok"] is False
    assert "client uid" in payload["error"]
    assert broker.called is False


def test_client_rejects_malformed_response_shapes_and_oversized_response(tmp_path):
    def one_response(payload: bytes) -> Path:
        sock_path = tmp_path / f"{len(payload)}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        sock_path.chmod(0o600)
        server.listen(1)

        def run():
            conn, _ = server.accept()
            with conn:
                while conn.recv(8):
                    pass
                for idx in range(0, len(payload), 5):
                    conn.sendall(payload[idx:idx + 5])
            server.close()

        threading.Thread(target=run).start()
        return sock_path

    with pytest.raises(protocol.ProtocolError):
        BrokerClient(one_response(b'{"ok":"yes","result":{}}')).call("gmail.search_messages", {})
    with pytest.raises(protocol.ProtocolError):
        BrokerClient(one_response(b'{"ok":true,"result":{},"extra":1}')).call("gmail.search_messages", {})
    with pytest.raises(protocol.ProtocolError):
        BrokerClient(one_response(b"x" * (protocol.MAX_MESSAGE_BYTES + 1))).call("gmail.search_messages", {})


def test_credentials_policy_rejects_before_google_imports(tmp_path, monkeypatch):
    rel = Path("relative.json")
    with pytest.raises(PermissionError):
        load_authorized_credentials(rel)

    cred = tmp_path / "cred.json"
    cred.write_text("{}", encoding="utf-8")
    cred.chmod(0o644)
    with pytest.raises(PermissionError):
        load_authorized_credentials(cred)

    cred.chmod(0o600)
    parent = tmp_path / "open"
    parent.mkdir()
    parent.chmod(0o777)
    bad_parent_cred = parent / "cred.json"
    bad_parent_cred.write_text("{}", encoding="utf-8")
    bad_parent_cred.chmod(0o600)
    with pytest.raises(PermissionError):
        load_authorized_credentials(bad_parent_cred)

    link = tmp_path / "cred-link.json"
    link.symlink_to(cred)
    with pytest.raises(PermissionError):
        load_authorized_credentials(link)

    def fail_import(*args, **kwargs):
        raise AssertionError("google imports must not run before credential policy passes")

    monkeypatch.setattr("builtins.__import__", fail_import)
    with pytest.raises(PermissionError):
        load_authorized_credentials(cred.with_name("missing.json"))


def test_credentials_loader_parses_open_fd_and_never_reopens_by_path(tmp_path, monkeypatch):
    cred = tmp_path / "cred.json"
    cred.write_text(json.dumps({"token": "tok", "refresh_token": "rt"}), encoding="utf-8")
    cred.chmod(0o600)
    calls = []

    class FakeCredentials:
        expired = False
        refresh_token = "rt"

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            calls.append(("info", info, tuple(scopes)))
            return cls()

        @classmethod
        def from_authorized_user_file(cls, filename, scopes=None):
            raise AssertionError("credential loader must not reopen by pathname")

    credentials_mod = types.ModuleType("google.oauth2.credentials")
    credentials_mod.Credentials = FakeCredentials
    oauth2_mod = types.ModuleType("google.oauth2")
    oauth2_mod.credentials = credentials_mod
    google_mod = types.ModuleType("google")
    google_mod.oauth2 = oauth2_mod
    requests_mod = types.ModuleType("google.auth.transport.requests")
    requests_mod.Request = lambda: object()
    transport_mod = types.ModuleType("google.auth.transport")
    transport_mod.requests = requests_mod
    auth_mod = types.ModuleType("google.auth")
    auth_mod.transport = transport_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_mod)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_mod)
    monkeypatch.setitem(sys.modules, "google.auth", auth_mod)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport_mod)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_mod)

    assert isinstance(load_authorized_credentials(cred), FakeCredentials)
    assert calls and calls[0][0] == "info"


def test_gmail_safe_label_assignment_reaches_backend_and_unsafe_never_does(tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path)
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))

    out = broker.dispatch({
        "operation": "gmail.modify_message_labels",
        "params": {"message_id": "m1", "add_label_ids": ["Label_123", "INBOX", "UNREAD"], "remove_label_ids": ["STARRED", "IMPORTANT"]},
    })
    assert out == {"id": "m1"}
    assert backend.calls[-1][0] == "modify_message_labels"

    for label in ["TRASH", "SPAM", "SENT", "DRAFT", "CATEGORY_UPDATES", "UNRECOGNIZED", "Label Name"]:
        backend.calls.clear()
        with pytest.raises(PolicyError):
            broker.dispatch({
                "operation": "gmail.modify_message_labels",
                "params": {"message_id": "m1", "add_label_ids": [label]},
            })
        assert backend.calls == []


def test_gmail_draft_and_read_params_are_validated_before_backend(tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path)
    backend = FakeBackend()
    broker = Broker(backend, CalendarOwnershipState(state_path))

    for operation, params in [
        ("gmail.search_messages", {"max_results": 0}),
        ("gmail.get_message", {"message_id": ""}),
        ("gmail.create_draft", {"to": ["a@example.com", ""], "subject": "s", "body_text": "b"}),
        ("gmail.create_draft", {"to": ["a@example.com"], "subject": "", "body_text": "b"}),
    ]:
        with pytest.raises(PolicyError):
            broker.dispatch({"operation": operation, "params": params})

    assert backend.calls == []


def test_calendar_state_owner_parent_malformed_and_preflight_before_backend(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    write_state(state)
    real_lstat = os.lstat
    real_stat = os.stat

    def wrong_owner_lstat(path):
        st = real_lstat(path)
        if Path(path) == state:
            values = list(st)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return st

    monkeypatch.setattr(os, "lstat", wrong_owner_lstat)
    with pytest.raises(PolicyError):
        CalendarOwnershipState(state).contains("x")
    monkeypatch.setattr(os, "lstat", real_lstat)

    def wrong_parent_owner_stat(path):
        st = real_stat(path)
        if Path(path) == tmp_path:
            values = list(st)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return st

    monkeypatch.setattr(os, "stat", wrong_parent_owner_stat)
    with pytest.raises(PolicyError):
        CalendarOwnershipState(state).contains("x")
    monkeypatch.setattr(os, "stat", real_stat)

    state.write_text("{bad", encoding="utf-8")
    backend = FakeBackend()
    with pytest.raises(PolicyError):
        Broker(backend, CalendarOwnershipState(state)).dispatch({
            "operation": "calendar.create_calendar",
            "params": {"summary": "must not reach backend"},
        })
    assert backend.calls == []


def test_error_sanitization_redacts_oauth_values_and_broker_hides_exception_details(tmp_path):
    text = protocol.sanitize_error(
        "Authorization: Bearer ya29.secret123 refresh_token=1//refresh access_token=abc.def ghi"
    )
    assert "Bearer" not in text
    assert "ya29" not in text
    assert "refresh" not in text
    assert "abc.def" not in text

    class LeakyBackend(FakeBackend):
        def search_messages(self, params):
            raise RuntimeError("Authorization: Bearer ya29.should-not-leak")

    state = tmp_path / "state.json"
    write_state(state)
    payload = json.loads(Broker(LeakyBackend(), CalendarOwnershipState(state)).handle_wire(
        protocol.encode_message("gmail.search_messages", {"query": "x"})
    ).decode("utf-8"))
    assert payload == {"ok": False, "error": "broker operation failed"}


def test_plugin_handler_hides_unexpected_exception_details(monkeypatch, tmp_path):
    calls = {}

    class Ctx:
        def register_tool(self, **kwargs):
            calls[kwargs["name"]] = kwargs

    gw_plugin.register(Ctx())
    monkeypatch.setenv("GOOGLE_WORKSPACE_BROKER_CONFIG", str(tmp_path / "config.json"))

    def boom(*args, **kwargs):
        raise ValueError("Authorization: Bearer ya29.must-not-leak")

    monkeypatch.setattr(gw_plugin.BrokerClient, "from_config_file", boom)
    result = json.loads(calls["google_gmail_read"]["handler"]({
        "operation": "gmail.search_messages",
        "query": "x",
    }))
    assert result == {"error": "google workspace broker call failed"}
