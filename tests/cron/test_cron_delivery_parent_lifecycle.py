"""Cron Bot Chat delivery is a child, never its firing Kanban worker."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('destination', ['', 'receiver'])
def test_bot_chat_delivery_cannot_spawn_as_parent(monkeypatch, tmp_path, destination):
    from cron import scheduler as sched
    from hermes_cli import profiles

    root = tmp_path / 'root'
    sender = root / 'profiles' / 'sender'
    receiver = root / 'profiles' / 'receiver'
    sender.mkdir(parents=True)
    receiver.mkdir(parents=True)
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: root)
    monkeypatch.setenv('HERMES_HOME', str(sender))
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_parent')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '42')
    monkeypatch.setattr(sched.shutil, 'which', lambda _: '/fixture/hermes')
    real_run = subprocess.run
    repo = str(Path(__file__).resolve().parents[2])
    seen = []

    def capture(argv, **kwargs):
        env = kwargs['env']
        assert not any(k.startswith('HERMES_KANBAN_') for k in env)
        code = (
            f'import sys; sys.path.insert(0, {repo!r}); '
            'from tools import kanban_tools as kt; '
            'assert kt._reject_delegated_child_mutation("kanban_complete") is not None'
        )
        result = real_run([sys.executable, '-c', code], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        seen.append(True)
        return subprocess.CompletedProcess(argv, 0, '', '')

    monkeypatch.setattr(sched.subprocess, 'run', capture)
    assert sched._deliver_to_bot_chat({'id': 'fixture'}, 'output', destination) is None
    assert seen == [True]
    assert os.environ['HERMES_KANBAN_TASK'] == 't_parent'
