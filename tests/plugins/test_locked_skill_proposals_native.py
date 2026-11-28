"""SDK-backed disposable native enrollment. No Telegram API or live profile access."""
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest

pytest_plugins = ['tests.hermes_cli.test_locked_skill_proposals']
pytestmark = pytest.mark.platforms('posix')


def test_enrollment_requires_native_capture_and_separate_confirmation(owned, monkeypatch):
    from telegram import Chat, Message, User
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageType
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.session_ingress_context import native_callback, register_transport_home
    from gateway.locked_skill_proposals import Proposals
    from hermes_state import SessionDB
    from hermes_state_runtime import RuntimeStoreError
    home, _, _ = owned
    with SessionDB(db_path=home / 'state.db') as db:
        authority = SimpleNamespace(profile_id=str(home), db=db)
        service = Proposals(authority)
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-only'))
        runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter}, session_authority=authority,
            config=SimpleNamespace(multiplex_profiles=False), _transport_owner=lambda src: (adapter, None),
            _adapter_credential_fingerprint=lambda item: 'fixture-connector')
        authority.runner = runner
        register_transport_home(runner, None, home)
        enrollment = service.enrollment_start('ops/example')
        def event(text, event_id=1, user_id=101, is_bot=False):
            raw = Message(message_id=event_id, date=datetime.now(UTC), chat=Chat(id=user_id, type='private'),
                          from_user=User(id=user_id, first_name='Fixture', is_bot=is_bot), text=text)
            return adapter._build_message_event(raw, MessageType.COMMAND)
        capture = event('/skills proposal register ' + enrollment['id'])
        with pytest.raises(RuntimeStoreError):
            service.native(runner, capture, ['register', enrollment['id']])
        with native_callback(runner, capture, home):
            captured = service.native(runner, capture, ['register', enrollment['id']])
        fingerprint = captured['candidate_digest']
        with pytest.raises(RuntimeStoreError):
            service.enrollment_finish(enrollment['id'], fingerprint)
        confirm = event('/skills proposal confirm ' + enrollment['id'] + ' ' + fingerprint, event_id=2)
        with native_callback(runner, confirm, home):
            assert service.native(runner, confirm, ['confirm', enrollment['id'], fingerprint])['confirmed']
        assert service.enrollment_finish(enrollment['id'], fingerprint)['registered']
        with pytest.raises(RuntimeStoreError):
            service.enrollment_start('ops/example')
        from hermes_cli import locked_skill_proposals as proposals
        from tests.hermes_cli.test_locked_skill_proposals import git, snapshot
        before = snapshot(home, home / 'skills/ops/example/SKILL.md')
        prepared = proposals.prepare(home=home, skill='ops/example', destination=owned[2])
        proposed = owned[2] / 'skills/ops/example/SKILL.md'
        proposed.write_text('Reviewed exact bytes\n')
        git(owned[2], 'add', 'skills/ops/example/SKILL.md')
        git(owned[2], 'commit', '-m', 'proposal')
        from tests.plugins.test_locked_skill_boundaries import synthetic_pull
        synthetic_pull(monkeypatch, home, owned[2], prepared)
        with pytest.raises(ValueError, match='PR'):
            service.freeze(prepared)
        frozen = service.freeze(prepared, pr_url='https://github.com/fixture/owner/pull/1')
        assert snapshot(home, owned[1]) == before
        with pytest.raises(RuntimeStoreError):
            service.apply(frozen['manifest_sha256'])
        approve = event('/skills proposal approve ' + frozen['manifest_sha256'], event_id=3)
        with pytest.raises(RuntimeStoreError):
            service.native(runner, approve, ['approve', frozen['manifest_sha256']])
        with native_callback(runner, approve, home):
            receipt = service.native(runner, approve, ['approve', frozen['manifest_sha256']])
        assert receipt['approved']
        assert snapshot(home, owned[1]) == before
        assert service.apply(frozen['manifest_sha256'])['applied']
        assert owned[1].read_bytes() == proposed.read_bytes()
        assert snapshot(home, owned[1])[1:] == before[1:]
        assert service.apply(frozen['manifest_sha256'])['already_applied']
