"""Explicit parent-only user-systemd harness. No bus probe without exact -k opt-in."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest


def test_parent_systemd_process(tmp_path, request):
    if request.config.option.keyword != "test_parent_systemd_process":
        pytest.skip("Parent only: explicitly select -k test_parent_systemd_process")
    owner = tmp_path / ".hermes" / "profiles" / "ang"
    release = owner / "state" / "release-switches" / "test-release"
    release.mkdir(parents=True, mode=0o700)
    for directory in (owner, release.parent, release.parent.parent):
        directory.chmod(0o700)
    output = release / "result.txt"
    go = tmp_path / "finish"
    ready = tmp_path / "ready"
    unit = f"hermes-item6-test-{uuid.uuid4().hex}.service"
    runtime = f"/run/user/{os.getuid()}"
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(owner),
               HERMES_KANBAN_DB=str(tmp_path / "board.db"),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]),
               XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus")
    child = tmp_path / "service.py"
    child.write_text(f'''import time
from pathlib import Path
Path({str(ready)!r}).touch()
end=time.monotonic()+40
while not Path({str(go)!r}).exists():
    if time.monotonic()>end: raise SystemExit(2)
    time.sleep(.02)
Path({str(output)!r}).write_bytes(b"Deterministic service output; not Codex execution.")
''')
    initiator = f'''import json,subprocess,time
from pathlib import Path
from hermes_cli import kanban_db as k
subprocess.run(["systemd-run","--user","--quiet","--unit",{unit!r},
    "--property=Type=exec","--property=RemainAfterExit=yes","--property=TasksAccounting=yes",
    "--property=RuntimeMaxSec=45","--property=UMask=0077", "--setenv=HERMES_HOME="+{str(owner)!r},
    {sys.executable!r},{str(child)!r}],check=True,timeout=10)
end=time.monotonic()+10
while not Path({str(ready)!r}).exists():
    if time.monotonic()>end: raise RuntimeError("service did not start")
    time.sleep(.02)
inv=subprocess.run(["systemctl","--user","show","--property=InvocationID","--value",{unit!r}],check=True,capture_output=True,text=True,timeout=5).stdout.strip()
c=k.connect(); card=k.create_task(c,title="Independent test service",assignee="ang",subscription_only=True,provider_override="openai-codex",model_override="gpt-6.1-sol")
r=k.claim_task(c,card)
wait=dict(next_owner="default",executor="ang",expected_effect="Inspect test receipt",resume_condition="result",recheck_at=int(time.time())+60,
 result_ref=dict(kind="external_process",unit={unit!r},invocation_id=inv,release_id="test-release",phase="source",candidate_sha="b"*40,known_good_sha="c"*40,result_basename="result.txt"))
assert k.schedule_task(c,card,reason="Independent service pending",expected_run_id=r.current_run_id,wait=wait)
assert k.reconcile_external_waits(c)==[]
print(json.dumps(dict(card=card,run=r.current_run_id,identity=k.get_run(c,r.current_run_id).metadata["external_process_identity"])));c.close()
'''
    try:
        result = subprocess.run([sys.executable, "-c", initiator], env=env, check=True,
                                capture_output=True, text=True, timeout=25)
        admitted = json.loads(result.stdout.strip().splitlines()[-1])
        # Native initiating interpreter has exited; actual service PID still lives.
        os.kill(admitted["identity"]["pid"], 0)
        go.touch()
        deadline = time.monotonic() + 15
        while True:
            state = subprocess.run(["systemctl", "--user", "show", "--property=SubState", "--value", unit],
                                   env=env, check=True, capture_output=True, text=True, timeout=5).stdout.strip()
            if state == "exited":
                break
            assert time.monotonic() < deadline, f"service failed to finish: {state}"
            time.sleep(.05)
        dispatch = f'''import json,os,hashlib
from pathlib import Path
from hermes_cli import kanban_db as k
c=k.connect(); calls=[]
def spawn(t,w,**kw):
    calls.append([t.assignee,t.model_override,t.provider_override,t.subscription_only]);return os.getpid()
k._memory_pressure_level=lambda:"normal"
k.dispatch_once(c,spawn_fn=spawn,max_spawn=1,board="default")
if not calls:
 from hermes_cli.kanban_external_process import _show, inspect_receipt
 run=k.get_run(c,{admitted['run']});wait=run.metadata['external_wait'];props=_show(wait['result_ref'],wait['executor']);props.pop('Environment',None)
 print(json.dumps(dict(props=props,identity=run.metadata['external_process_identity'],inspection=inspect_receipt(wait,identity=run.metadata['external_process_identity']))),file=__import__('sys').stderr)
assert calls==[["default","gpt-6.1-sol","openai-codex",True]],calls
receipt=k.get_run(c,{admitted['run']}).metadata["external_process_receipt"]
assert receipt["result_sha256"]==hashlib.sha256(Path({str(output)!r}).read_bytes()).hexdigest()
assert "result_preview" not in receipt
before=k.list_events(c,{admitted['card']!r});assert k.reconcile_external_waits(c)==[];assert before==k.list_events(c,{admitted['card']!r})
print(json.dumps(dict(calls=calls,receipt=receipt)));c.close()
'''
        completed = subprocess.run([sys.executable, "-c", dispatch], env=env, check=False,
                                   capture_output=True, text=True, timeout=25)
        assert completed.returncode == 0, completed.stderr
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        assert proof["receipt"]["verdict"] == "process_exited"
    finally:
        # Only this UUID-named test unit; never a gateway/profile service.
        subprocess.run(["systemctl", "--user", "stop", unit], env=env, capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], env=env, capture_output=True, timeout=10)
