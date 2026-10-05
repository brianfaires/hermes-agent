"""Read-only receipts for explicitly bound, independently supervised CLI runs.

No launch, discovery, polling daemon, or release authority lives here. The
caller freezes the reference in the initiating native run's external wait.
"""
import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from hermes_cli import profiles

MAX_RESULT_BYTES = 1024 * 1024
_UID = getattr(os, "getuid", lambda: None)()
_FIELDS = {"kind", "unit", "invocation_id", "release_id", "phase", "candidate_sha",
           "known_good_sha", "result_basename"}


def validate_ref(ref):
    if set(ref) != _FIELDS or ref.get("kind") != "external_process":
        raise ValueError("external_process requires unit, invocation_id, release_id, phase, candidate_sha, known_good_sha, result_basename")
    patterns = {
        "unit": r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,180}\.service",
        "invocation_id": r"[0-9a-f]{32}",
        "candidate_sha": r"[0-9a-f]{40}|[0-9a-f]{64}",
        "known_good_sha": r"[0-9a-f]{40}|[0-9a-f]{64}",
        "release_id": r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",
        "phase": r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
        "result_basename": r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
    }
    for key, pattern in patterns.items():
        if not isinstance(ref[key], str) or not re.fullmatch(pattern, ref[key]):
            raise ValueError(f"invalid external_process {key}")


@contextmanager
def _release_dir(owner, ref):
    # Walk from the first real component (hardened hosts deny opening /).
    # Held dirfds ensure no ancestor or final-component symlink can
    # escape the owner directory, including during rename/symlink races.
    home = profiles.get_profile_dir(owner).absolute()
    path = home / "state" / "release-switches" / ref["release_id"]
    fd = os.open("/" + path.parts[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(path.parts[2:], 2):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if index >= len(home.parts) - 1:
                if info.st_uid != _UID or info.st_mode & 0o022:
                    raise ValueError("executor directory is not owner-controlled")
            if index > len(home.parts) - 1 and info.st_mode & 0o077:
                raise ValueError("release state directories must be private")
        yield fd
    finally:
        os.close(fd)


def _show(ref, owner):
    names = "Id,LoadState,Transient,InvocationID,ActiveState,SubState,MainPID,ExecMainPID,ControlGroup,TasksCurrent,Result,ExecMainCode,ExecMainStatus,ExecMainStartTimestampMonotonic,Environment"
    result = subprocess.run(
        ["systemctl", "--user", "show", "--no-pager", f"--property={names}", "--", ref["unit"]],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError("supervisor unavailable")
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    env = shlex.split(props.get("Environment", ""))
    if (props.get("Id") != ref["unit"] or props.get("LoadState") != "loaded"
            or props.get("Transient") != "yes" or props.get("InvocationID") != ref["invocation_id"]
            or [v for v in env if v.startswith("HERMES_HOME=")] != [f"HERMES_HOME={profiles.get_profile_dir(owner).absolute()}"]):
        raise ValueError("supervisor identity or executor profile mismatch")
    return props


def _proc_identity(pid):
    """Read exact Linux process identity, checking for death/reuse during read."""
    root = Path("/proc") / str(int(pid))
    first = (root / "stat").read_text(encoding="utf-8")
    fields = first[first.rfind(")") + 2:].split()
    groups = (root / "cgroup").read_text(encoding="utf-8").splitlines()
    cgroups = [line.split(":", 2)[2] for line in groups
               if line.startswith("0::") or line.split(":", 2)[1] == "name=systemd"]
    if (len(cgroups) != 1 or fields[0] in {"Z", "X", "x"}
            or root.stat().st_uid != _UID
            or (root / "stat").read_text(encoding="utf-8").rpartition(") ")[2].split()[19] != fields[19]):
        raise ValueError("unavailable or changing process identity")
    return dict(pid=int(pid), start_ticks=int(fields[19]), pgrp=int(fields[2]), cgroup=cgroups[0])


def _live_identity(props, control_pid=None):
    pid = int(props["MainPID"])
    actual = _proc_identity(pid)
    controllers = [_proc_identity(os.getpid())]
    if control_pid is not None and control_pid != os.getpid():
        controllers.append(_proc_identity(control_pid))
    group = props["ControlGroup"]
    if (pid <= 0 or int(props["ExecMainPID"]) != pid
            or not group.startswith("/") or group == "/" or actual["cgroup"] != group
            or any(actual["pgrp"] == controller["pgrp"]
                   or group == controller["cgroup"]
                   or group.startswith(controller["cgroup"].rstrip("/") + "/")
                   or controller["cgroup"].startswith(group.rstrip("/") + "/")
                   for controller in controllers)):
        raise ValueError("executor is not an independent service process")
    started = int(props["ExecMainStartTimestampMonotonic"])
    actual_us = actual["start_ticks"] * 1_000_000 // os.sysconf("SC_CLK_TCK")
    if started <= 0 or abs(actual_us - started) > 1_000_000:
        raise ValueError("supervisor process start mismatch")
    return {**actual, "unit": props["Id"], "invocation_id": props["InvocationID"],
            "supervisor_start_us": started}


def inspect_receipt(wait, *, require_live=False, identity=None, control_pid=None):
    """Return (binding valid, terminal/liveness receipt or None), fail closed."""
    if sys.platform != "linux" or _UID is None:
        return False, None
    ref, owner = wait["result_ref"], wait["executor"]
    try:
        validate_ref(ref)
        if not profiles.profile_exists(owner):
            return False, None
        with _release_dir(owner, ref) as directory:
            props = _show(ref, owner)
            live = (props.get("ActiveState") == "active" and props.get("SubState") == "running"
                    and int(props.get("MainPID", "0")) > 0)
            if require_live:
                if not live:
                    return False, None
                binding = _live_identity(props, control_pid)
                if _show(ref, owner) != props or _live_identity(props, control_pid) != binding:
                    return False, None
                return True, binding
            released_group = (
                (props.get("ActiveState"), props.get("SubState")) in {
                    ("inactive", "dead"), ("failed", "failed"), ("active", "exited")
                }
                and props.get("MainPID") == "0"
                and props.get("ControlGroup") == ""
                and props.get("TasksCurrent") == "[not set]"
            )
            if (not isinstance(identity, dict)
                    or identity.get("unit") != props["Id"]
                    or identity.get("invocation_id") != props["InvocationID"]
                    or (identity.get("cgroup") != props["ControlGroup"] and not released_group)
                    or identity.get("pid") != int(props["ExecMainPID"])
                    or identity.get("supervisor_start_us") != int(props["ExecMainStartTimestampMonotonic"])):
                return False, None
            if live and _live_identity(props) != identity:
                return False, None
            if wait["resume_condition"] != "result":
                return False, None  # A live executor never enables owner transfer.
            if (props.get("ActiveState"), props.get("SubState")) not in {
                ("inactive", "dead"), ("failed", "failed"), ("active", "exited")
            } or props.get("MainPID") != "0":
                return True, None
            # RemainAfterExit keeps a successful unit available for inspection.
            # A surviving child is still an active executor, even with MainPID=0.
            tasks = props.get("TasksCurrent")
            if tasks != "0" and not released_group:
                return True, None
            code, status = int(props["ExecMainCode"]), int(props["ExecMainStatus"])
            if code not in (1, 2, 3):  # exited, killed, dumped; 0 is no execution
                return False, None
            receipt = dict(unit=ref["unit"], invocation_id=ref["invocation_id"],
                           unit_result=props["Result"], exit_code=code, exit_status=status,
                           verdict="process_exited" if code == 1 and status == 0 and props["Result"] == "success" else "process_failed")
            try:
                started = int(props["ExecMainStartTimestampMonotonic"]) * 1000
                monotonic_now = time.monotonic_ns()
                if not 0 < started <= monotonic_now:
                    raise ValueError("missing supervisor start time")
                # Do not accept a previous invocation's file in a reused release
                # directory. Clock discontinuities conservatively reject output.
                started_wall = time.time_ns() - monotonic_now + started
                fd = os.open(ref["result_basename"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_uid != _UID
                            or before.st_mode & 0o077 or before.st_nlink != 1
                            or not 0 < before.st_size <= MAX_RESULT_BYTES
                            or before.st_mtime_ns < started_wall):
                        raise ValueError("result must be a bounded private regular file")
                    data = stream.read(MAX_RESULT_BYTES + 1)
                    after = os.fstat(stream.fileno())
                    if (len(data) != before.st_size or before.st_mtime_ns != after.st_mtime_ns
                            or before.st_ctime_ns != after.st_ctime_ns or before.st_size != after.st_size):
                        raise ValueError("result changed during read")
                receipt.update(result_sha256=hashlib.sha256(data).hexdigest(), result_bytes=len(data))
            except (OSError, ValueError):
                # Crashes without output are still failures, never results.
                if receipt["verdict"] != "process_failed":
                    return False, None
                receipt["result_error"] = "private_result_unavailable"
            if _show(ref, owner) != props:
                return False, None
            return True, receipt
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return False, None
