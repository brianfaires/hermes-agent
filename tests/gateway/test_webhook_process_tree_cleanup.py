import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_filters import WebhookRouteProcessor


pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression"),
    pytest.mark.live_system_guard_bypass,
]


def _write_spawning_script(script: Path, pid_file: Path) -> None:
    script.write_text(
        "\n".join(
            [
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                "child = subprocess.Popen(",
                "    [sys.executable, '-c', 'import time; time.sleep(60)'],",
                "    stdin=subprocess.DEVNULL,",
                "    stdout=subprocess.DEVNULL,",
                "    stderr=subprocess.DEVNULL,",
                ")",
                f"Path({str(pid_file)!r}).write_text(str(child.pid))",
                "time.sleep(60)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _read_pid(pid_file: Path, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text().strip())
        time.sleep(0.01)
    raise AssertionError("child pid was not recorded")


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_process_gone(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.01)
    return not _process_exists(pid)


def _cleanup_process(pid: int) -> None:
    if not _process_exists(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _wait_process_gone(pid)


def test_transform_script_timeout_kills_descendant_processes(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "spawn_child.py"
    pid_file = tmp_path / "transform-child.pid"
    _write_spawning_script(script, pid_file)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    processor = WebhookRouteProcessor(script_timeout_seconds=1)
    child_pid = None
    try:
        assert processor.run_route_script(script.name, {"event": "test"}) == (False, None)
        child_pid = _read_pid(pid_file)
        assert _wait_process_gone(child_pid), "transform timeout leaked a descendant"
    finally:
        if child_pid is not None:
            _cleanup_process(child_pid)


def test_transform_communication_error_kills_and_reaps_process(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "broken.py"
    script.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    class BrokenProc:
        pid = 424242
        stdin = None
        stdout = None
        stderr = None
        returncode = None

        def __init__(self):
            self.killed = False
            self.waited = False

        def communicate(self, **_kwargs):
            raise OSError("pipe failed")

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            self.waited = True
            return self.returncode

    proc = BrokenProc()
    killed_groups = []
    monkeypatch.setattr("gateway.platforms.webhook_filters.subprocess.Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(
        "gateway.platforms.webhook_filters._kill_script_process_group",
        lambda pid: killed_groups.append(pid),
    )

    processor = WebhookRouteProcessor(script_timeout_seconds=1)
    assert processor.run_route_script(script.name, {"event": "test"}) == (False, None)
    assert killed_groups == [proc.pid]
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_trigger_script_timeout_kills_descendant_processes(tmp_path):
    script = tmp_path / "spawn_child.py"
    pid_file = tmp_path / "trigger-child.pid"
    _write_spawning_script(script, pid_file)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))

    child_pid = None
    try:
        await adapter._run_script_trigger(
            route_name="test",
            script_path=script,
            delivery_id="delivery-1",
            timeout=0.5,
        )
        child_pid = _read_pid(pid_file)
        assert _wait_process_gone(child_pid), "trigger timeout leaked a descendant"
    finally:
        if child_pid is not None:
            _cleanup_process(child_pid)


@pytest.mark.asyncio
async def test_trigger_script_cancellation_kills_descendant_processes(tmp_path):
    script = tmp_path / "spawn_child.py"
    pid_file = tmp_path / "cancelled-trigger-child.pid"
    _write_spawning_script(script, pid_file)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))

    child_pid = None
    task = asyncio.create_task(
        adapter._run_script_trigger(
            route_name="test",
            script_path=script,
            delivery_id="delivery-cancelled",
            timeout=60,
        )
    )
    try:
        child_pid = await asyncio.to_thread(_read_pid, pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _wait_process_gone(child_pid), "trigger cancellation leaked a descendant"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child_pid is not None:
            _cleanup_process(child_pid)


@pytest.mark.asyncio
async def test_trigger_communication_error_kills_and_reaps_process(tmp_path, monkeypatch):
    script = tmp_path / "broken.py"
    script.write_text("pass\n", encoding="utf-8")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))

    class BrokenAsyncProc:
        pid = 424243
        returncode = None

        def __init__(self):
            self.killed = False
            self.waited = False

        async def communicate(self):
            raise OSError("pipe failed")

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    proc = BrokenAsyncProc()
    killed_groups = []
    monkeypatch.setattr(
        "gateway.platforms.webhook.asyncio.create_subprocess_exec",
        lambda *_a, **_k: asyncio.sleep(0, result=proc),
    )
    monkeypatch.setattr(
        "gateway.platforms.webhook._kill_script_process_group",
        lambda pid: killed_groups.append(pid),
    )

    await adapter._run_script_trigger(
        route_name="test",
        script_path=script,
        delivery_id="delivery-failed",
        timeout=60,
    )
    assert killed_groups == [proc.pid]
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_trigger_cancellation_during_spawn_tracks_and_cleans_process(
    tmp_path, monkeypatch
):
    script = tmp_path / "slow-spawn.py"
    script.write_text("pass\n", encoding="utf-8")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    spawn_cancelled = asyncio.Event()

    class SpawnedProc:
        pid = 424244
        returncode = None

        def __init__(self):
            self.killed = False
            self.waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    proc = SpawnedProc()
    killed_groups = []

    async def delayed_spawn(*_args, **_kwargs):
        spawn_started.set()
        try:
            await release_spawn.wait()
        except asyncio.CancelledError:
            spawn_cancelled.set()
            raise
        return proc

    monkeypatch.setattr(
        "gateway.platforms.webhook.asyncio.create_subprocess_exec",
        delayed_spawn,
    )
    monkeypatch.setattr(
        "gateway.platforms.webhook._kill_script_process_group",
        lambda pid: killed_groups.append(pid),
    )

    task = asyncio.create_task(
        adapter._run_script_trigger(
            route_name="test",
            script_path=script,
            delivery_id="delivery-spawn-cancelled",
            timeout=60,
        )
    )
    await spawn_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawn_cancelled.is_set() is False
    assert killed_groups == [proc.pid]
    assert proc.killed is True
    assert proc.waited is True
