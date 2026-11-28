"""Real SDK/native callback boundary and isolated exact-file recovery tests."""
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_locked_skill_proposals import git, snapshot

pytest_plugins = ['tests.hermes_cli.test_locked_skill_proposals']
pytestmark = pytest.mark.platforms('posix')


def synthetic_pull(monkeypatch, home, destination, prepared):
    """Only GitHub's read-only metadata is substituted; never human approval."""
    from hermes_cli import locked_skill_publication
    git(home, 'remote', 'add', 'origin', 'https://github.com/fixture/owner.git')
    metadata = {'html_url': 'https://github.com/fixture/owner/pull/1', 'state': 'open', 'merged': False,
                'head': {'repo': {'full_name': 'fixture/owner'}, 'sha': git(destination, 'rev-parse', 'HEAD'), 'ref': prepared['branch']},
                'base': {'repo': {'full_name': 'fixture/owner'}, 'sha': prepared['base'], 'ref': 'main'}}
    monkeypatch.setattr(locked_skill_publication, '_read_pull', lambda repository, number, **kwargs: metadata)
    return metadata


@pytest.fixture
def workflow(owned, monkeypatch):
    from telegram import Chat, Message, User
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageType
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.session_ingress_context import native_callback, register_transport_home
    from gateway.locked_skill_proposals import Proposals
    from hermes_state import SessionDB
    from hermes_cli import locked_skill_proposals as proposals

    home, skill, destination = owned
    with SessionDB(db_path=home / 'state.db') as db:
        authority = SimpleNamespace(profile_id=str(home), db=db, _require_admission_open=lambda: None)
        service = Proposals(authority)
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-only'))
        runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter}, session_authority=authority,
            config=SimpleNamespace(multiplex_profiles=False), _transport_owner=lambda src: (adapter, None),
            _adapter_credential_fingerprint=lambda item: 'fixture-connector')
        authority.runner = runner
        register_transport_home(runner, None, home)
        counter = iter(range(1, 100))
        def event(args, *, sender=101, bot=False):
            raw = Message(message_id=next(counter), date=datetime.now(UTC), chat=Chat(id=sender, type='private'),
                          from_user=User(id=sender, first_name='Synthetic', is_bot=bot),
                          text='/skills proposal ' + ' '.join(args))
            return adapter._build_message_event(raw, MessageType.COMMAND)
        def native(args, **kwargs):
            received = event(args, **kwargs)
            with native_callback(runner, received, home):
                return service.native(runner, received, args)
        enrollment = service.enrollment_start('ops/example')
        captured = native(['register', enrollment['id']])
        native(['confirm', enrollment['id'], captured['candidate_digest']])
        service.enrollment_finish(enrollment['id'], captured['candidate_digest'])
        prepared = proposals.prepare(home=home, skill='ops/example', destination=destination)
        (destination / 'skills/ops/example/SKILL.md').write_text('Reviewed exact bytes\n')
        git(destination, 'add', 'skills/ops/example/SKILL.md')
        git(destination, 'commit', '-m', 'proposal')
        pull = synthetic_pull(monkeypatch, home, destination, prepared)
        manifest = service.freeze(prepared, pr_url='https://github.com/fixture/owner/pull/1')
        yield SimpleNamespace(home=home, skill=skill, destination=destination, service=service, pull=pull,
                              runner=runner, event=event, native=native, manifest=manifest,
                              fingerprint=manifest['manifest_sha256'])


@pytest.mark.parametrize('kind', ['absent', 'wrong-human', 'bot', 'no-callback', 'forged-source', 'changed-text'])
def test_invalid_native_approval_leaves_target_and_receipt_unchanged(workflow, kind):
    from gateway.session_ingress_context import native_callback
    from hermes_state_runtime import RuntimeStoreError
    case = workflow
    args = ['approve', case.fingerprint]
    before = snapshot(case.home, case.skill)
    if kind == 'absent':
        with pytest.raises(RuntimeStoreError):
            case.service.apply(case.fingerprint)
    elif kind in {'wrong-human', 'bot'}:
        with pytest.raises(RuntimeStoreError):
            case.native(args, sender=202 if kind == 'wrong-human' else 101, bot=kind == 'bot')
    else:
        event = case.event(args, sender=202 if kind == 'forged-source' else 101)
        if kind == 'forged-source':
            event.source.user_id = event.source.chat_id = '101'
        if kind == 'changed-text':
            event.text = '/skills proposal approve not-the-original-text'
        with pytest.raises(RuntimeStoreError):
            if kind == 'no-callback':
                case.service.native(case.runner, event, args)
            else:
                with native_callback(case.runner, event, case.home):
                    case.service.native(case.runner, event, args)
    assert snapshot(case.home, case.skill) == before
    from gateway.locked_skill_proposals import _read
    with case.service.db._read_ctx() as conn:
        assert _read(conn, 'approval:' + case.fingerprint) is None


@pytest.mark.parametrize('mutation', ['live-bytes', 'head', 'proposal', 'lock-missing', 'lock-corrupt', 'lock-unreadable', 'symlink', 'mode'])
def test_apply_rechecks_approved_identity(workflow, mutation):
    case = workflow
    case.native(['approve', case.fingerprint])
    lock = case.home / 'skills/.usage.json'
    def advance():
        (case.home / 'other').write_text('unrelated commit')
        git(case.home, 'add', 'other')
        git(case.home, 'commit', '-m', 'advanced')
    def redirect():
        sibling = case.skill.with_name('sibling')
        sibling.write_bytes(case.skill.read_bytes())
        case.skill.unlink()
        case.skill.symlink_to(sibling)
    changes = {
        'live-bytes': lambda: case.skill.write_bytes(b'concurrent live change\n'),
        'head': advance,
        'proposal': lambda: (case.destination / 'skills/ops/example/SKILL.md').write_text('changed proposed bytes'),
        'lock-missing': lock.unlink, 'lock-corrupt': lambda: lock.write_text('not JSON'),
        'lock-unreadable': lambda: lock.chmod(0), 'mode': lambda: case.skill.chmod(0o755),
        'symlink': redirect,
    }
    changes[mutation]()
    before = case.skill.read_bytes()
    with pytest.raises((ValueError, OSError)):
        case.service.apply(case.fingerprint)
    assert case.skill.read_bytes() == before


def test_write_before_receipt_recovers_exact_owned_inode(workflow, monkeypatch):
    from gateway import locked_skill_apply
    case = workflow
    case.native(['approve', case.fingerprint])
    def interruption(*args):
        raise RuntimeError('synthetic interruption after replacement')
    with monkeypatch.context() as scoped:
        scoped.setattr(locked_skill_apply, '_record', interruption)
        with pytest.raises(RuntimeError, match='synthetic interruption'):
            case.service.apply(case.fingerprint)
    assert case.skill.read_bytes() == b'Reviewed exact bytes\n'
    result = case.service.apply(case.fingerprint)
    assert result['recovered']
    assert case.service.apply(case.fingerprint)['already_applied']


def test_write_before_receipt_never_replays_against_changed_bytes(workflow, monkeypatch):
    from gateway import locked_skill_apply
    case = workflow
    case.native(['approve', case.fingerprint])
    with monkeypatch.context() as scoped:
        def interruption(*args):
            raise RuntimeError('synthetic interruption')
        scoped.setattr(locked_skill_apply, '_record', interruption)
        with pytest.raises(RuntimeError):
            case.service.apply(case.fingerprint)
    case.skill.write_bytes(b'newer content\n')
    with pytest.raises(ValueError):
        case.service.apply(case.fingerprint)
    assert case.skill.read_bytes() == b'newer content\n'


def test_apply_refuses_a_changed_native_connector(workflow):
    case = workflow
    case.native(['approve', case.fingerprint])
    case.runner._adapter_credential_fingerprint = lambda item: 'different-connector'
    before = snapshot(case.home, case.skill)
    with pytest.raises(ValueError):
        case.service.apply(case.fingerprint)
    assert snapshot(case.home, case.skill) == before


def test_scoped_reconciliation_preserves_dirty_and_staged_unrelated_state(workflow):
    case = workflow
    case.native(['approve', case.fingerprint])
    case.service.apply(case.fingerprint)
    unrelated = case.home / 'config.yaml'
    unrelated.write_text('unrelated staged state\n')
    git(case.home, 'add', 'config.yaml')
    unrelated.write_text('unrelated newer unstaged state\n')
    staged_before = git(case.home, 'show', ':config.yaml')
    lock_before = (case.home / 'skills/.usage.json').read_bytes()
    accepted = case.service.reconcile(case.fingerprint)
    assert accepted['head'] == case.manifest['head']
    assert git(case.home, 'rev-parse', 'HEAD') == case.manifest['head']
    assert git(case.home, 'show', ':config.yaml') == staged_before
    assert unrelated.read_text() == 'unrelated newer unstaged state\n'
    assert (case.home / 'skills/.usage.json').read_bytes() == lock_before
    assert case.service.reconcile(case.fingerprint)['already_reconciled']
    assert case.service.apply(case.fingerprint)['already_applied']


@pytest.mark.parametrize('field', ['head', 'base', 'repository', 'closed', 'origin'])
def test_changed_pr_identity_invalidates_native_approval_and_apply(workflow, field):
    case = workflow
    case.native(['approve', case.fingerprint])
    changes = {
        'head': lambda: case.pull['head'].update(sha='0' * 40),
        'base': lambda: case.pull['base'].update(sha='0' * 40),
        'repository': lambda: case.pull['head']['repo'].update(full_name='fixture/other'),
        'closed': lambda: case.pull.update(state='closed'),
        'origin': lambda: git(case.home, 'remote', 'set-url', 'origin', 'https://github.com/fixture/other.git'),
    }
    changes[field]()
    before = snapshot(case.home, case.skill)
    with pytest.raises(ValueError):
        case.native(['approve', case.fingerprint])
    with pytest.raises(ValueError):
        case.service.apply(case.fingerprint)
    assert snapshot(case.home, case.skill) == before


def test_fabricated_file_receipt_is_not_approval(workflow):
    case = workflow
    (case.home / 'approval.json').write_text('{"approved":true,"authorized_by":"Brian"}')
    before = snapshot(case.home, case.skill)
    with pytest.raises(ValueError):
        case.service.apply(case.fingerprint)
    assert snapshot(case.home, case.skill) == before


def test_concurrent_identical_apply_has_one_replacement(workflow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    case = workflow
    case.native(['approve', case.fingerprint])
    barrier = Barrier(2)
    def apply():
        barrier.wait(timeout=10)
        return case.service.apply(case.fingerprint)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: apply(), range(2)))
    assert sum(result.get('already_applied', False) for result in results) == 1
    assert case.skill.read_bytes() == b'Reviewed exact bytes\n'
