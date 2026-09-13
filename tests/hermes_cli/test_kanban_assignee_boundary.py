"""Assignee validation precedes all parent-side worker setup."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_constants import mark_named_profile_deleted


def task(assignee):
    return kb.Task(id='t_fixture', title='fixture', body=None, assignee=assignee,
                   status='running', priority=0, created_by='fixture', created_at=1,
                   started_at=None, completed_at=None, workspace_kind='dir',
                   workspace_path=None, claim_lock='fixture-lock', claim_expires=None,
                   tenant=None, current_run_id=7)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path / 'custom root'; root.mkdir()
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    return root, workspace


@pytest.mark.parametrize('assignee,state,error', [
    ('missing', 'missing', FileNotFoundError), ('deleted', 'deleted', FileNotFoundError),
    ('../escape', 'missing', ValueError), ('', 'missing', ValueError),
])
def test_invalid_assignee_stops_before_parent_setup(layout, monkeypatch, assignee, state, error):
    root, workspace = layout
    if state == 'deleted':
        home = root / 'profiles' / assignee; home.mkdir(parents=True)
        mark_named_profile_deleted(home)
    def forbidden(*a, **kw):
        pytest.fail('parent worker setup reached after failed owner resolution')
    for name in ('_retag_legacy_worker_sessions', '_resolve_worker_cli_toolsets',
                 'worker_logs_dir', 'worker_log_rotation_config', '_resolve_hermes_argv',
                 'kanban_db_path', 'workspaces_root'):
        monkeypatch.setattr(kb, name, forbidden)
    monkeypatch.setattr(kb.subprocess, 'Popen', forbidden)
    with pytest.raises(error):
        kb._default_spawn(task(assignee), str(workspace), board='fixture')
    assert not (root / 'kanban').exists()


def test_valid_assignee_keeps_real_config_board_workspace_pins(layout, monkeypatch):
    root, workspace = layout
    home = root / 'profiles' / 'worker'; home.mkdir(parents=True)
    (home / 'config.yaml').write_text('platform_toolsets:\n  cli: [file, terminal]\n')
    monkeypatch.setenv('HERMES_SESSION_CHAT_ID', 'fake-sender')
    monkeypatch.setattr(kb, '_resolve_hermes_argv', lambda: ['hermes'])
    captured = {}
    def spawn(argv, **kw):
        captured.update(argv=argv, **kw)
        return SimpleNamespace(pid=4242)
    monkeypatch.setattr(kb.subprocess, 'Popen', spawn)
    assert kb._default_spawn(task('worker'), str(workspace), board='fixture') == 4242
    env = captured['env']
    assert env['HERMES_HOME'] == str(home)
    assert env['HERMES_PROFILE'] == 'worker'
    assert env['HERMES_KANBAN_BOARD'] == 'fixture'
    assert Path(env['HERMES_KANBAN_DB']) == kb.kanban_db_path(board='fixture')
    assert Path(env['HERMES_KANBAN_WORKSPACES_ROOT']) == kb.workspaces_root(board='fixture')
    assert env['HERMES_KANBAN_WORKSPACE'] == str(workspace)
    assert captured['cwd'] == str(workspace)
    assert 'HERMES_SESSION_CHAT_ID' not in env
    pinned = captured['argv'][captured['argv'].index('--toolsets') + 1].split(',')
    assert {'file', 'terminal'} <= set(pinned)
