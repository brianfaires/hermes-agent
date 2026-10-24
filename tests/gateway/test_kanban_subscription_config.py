"""The auto-subscription preference covers slash creation and preserves explicit routes."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn


@pytest.mark.asyncio
async def test_slash_create_respects_off_without_disabling_explicit_subscriptions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    home = tmp_path / '.hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  auto_subscribe_on_create: false\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    kb.create_board('fixture')
    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id='general-chat', chat_type='dm',
                             thread_id=None, user_id='1', profile=None)
    event = SimpleNamespace(text='/kanban --board fixture create "Explicit route" --assignee default',
        source=source, message_id='7', reply_to_message_id=None)
    with _profile_runtime_scope(home):
        output = await runner._handle_kanban_command(event)
    assert 'Created' in output
    with kbc.connect_closing(board='fixture') as conn:
        tasks = kb.list_tasks(conn)
        assert len(tasks) == 1
        assert kbn.list_notify_subs(conn, tasks[0].id) == []
    event.text = ('/kanban --board fixture notify-subscribe ' + tasks[0].id
        + ' --platform telegram --chat-id owner-home --user-id 1 --chat-type dm'
        + ' --notifier-profile default --delivery-mode notify')
    with _profile_runtime_scope(home):
        await runner._handle_kanban_command(event)
    with kbc.connect_closing(board='fixture') as conn:
        subscriptions = kbn.list_notify_subs(conn, tasks[0].id)
        assert len(subscriptions) == 1
        assert subscriptions[0]['chat_id'] == 'owner-home'
        assert subscriptions[0]['notifier_profile'] == 'default'
        assert subscriptions[0]['delivery_mode'] == 'notify'
