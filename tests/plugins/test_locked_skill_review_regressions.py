"""Regressions for independently reviewed locking/recovery/telemetry boundaries."""
import subprocess

import pytest

from tests.hermes_cli.test_locked_skill_proposals import git, snapshot

pytest_plugins = ['tests.plugins.test_locked_skill_boundaries']
pytestmark = pytest.mark.platforms('posix')


@pytest.mark.parametrize('interrupted', [False, True])
def test_unrelated_telemetry_never_strands_exact_apply_or_recovery(workflow, monkeypatch, interrupted):
    from gateway import locked_skill_apply
    from tools import skill_usage
    case = workflow
    case.native(['approve', case.fingerprint])
    if interrupted:
        with monkeypatch.context() as scoped:
            def interruption(*args):
                raise RuntimeError('synthetic write-before-receipt interruption')
            scoped.setattr(locked_skill_apply, '_record', interruption)
            with pytest.raises(RuntimeError):
                case.service.apply(case.fingerprint)
    else:
        case.service.apply(case.fingerprint)
    skill_usage.bump_view('unrelated-skill')
    result = case.service.apply(case.fingerprint)
    assert result.get('recovered') if interrupted else result['already_applied']
    assert case.service.reconcile(case.fingerprint)['completed']
    assert case.skill.read_bytes() == b'Reviewed exact bytes\n'


def test_completed_reconciliation_refuses_reverse_target_staging(workflow):
    case = workflow
    case.native(['approve', case.fingerprint])
    case.service.apply(case.fingerprint)
    case.service.reconcile(case.fingerprint)
    git(case.home, 'update-index', '--cacheinfo', case.manifest['mode'], case.manifest['pre_blob'], case.manifest['path'])
    staged = git(case.home, 'ls-files', '--stage', '--', case.manifest['path'])
    with pytest.raises(ValueError):
        case.service.reconcile(case.fingerprint)
    assert git(case.home, 'ls-files', '--stage', '--', case.manifest['path']) == staged


def test_target_staging_interleaving_is_never_silently_overwritten(workflow, monkeypatch):
    from gateway import locked_skill_reconcile
    case = workflow
    case.native(['approve', case.fingerprint])
    case.service.apply(case.fingerprint)
    concurrent_file = case.home / 'concurrent-candidate'
    concurrent_file.write_text('concurrent staged content\n')
    other_blob = git(case.home, 'hash-object', '-w', 'concurrent-candidate')
    original = locked_skill_reconcile.git
    outcome = {'attempted': False, 'writer_won': False}
    def interleave(scope, *args, **kwargs):
        if args[0] == 'update-index' and not outcome['attempted']:
            outcome['attempted'] = True
            try:
                git(case.home, 'update-index', '--cacheinfo', case.manifest['mode'], other_blob, case.manifest['path'])
                outcome['writer_won'] = True
            except subprocess.CalledProcessError as exc:
                assert b'index.lock' in exc.output
        return original(scope, *args, **kwargs)
    monkeypatch.setattr(locked_skill_reconcile, 'git', interleave)
    refused = False
    try:
        case.service.reconcile(case.fingerprint)
    except ValueError:
        refused = True
    assert outcome['attempted']
    if outcome['writer_won']:
        assert refused
        assert other_blob in git(case.home, 'ls-files', '--stage', '--', case.manifest['path'])
    else:
        assert not refused
        assert case.manifest['post_blob'] in git(case.home, 'ls-files', '--stage', '--', case.manifest['path'])


def test_recovery_refuses_identical_bytes_on_an_unowned_replacement_inode(workflow, monkeypatch):
    from gateway import locked_skill_apply
    case = workflow
    case.native(['approve', case.fingerprint])
    with monkeypatch.context() as scoped:
        def interruption(*args):
            raise RuntimeError('synthetic write-before-receipt interruption')
        scoped.setattr(locked_skill_apply, '_record', interruption)
        with pytest.raises(RuntimeError):
            case.service.apply(case.fingerprint)
    previous_inode = case.skill.stat().st_ino
    replacement = case.skill.with_name('replacement')
    replacement.write_bytes(case.skill.read_bytes())
    replacement.replace(case.skill)
    assert case.skill.stat().st_ino != previous_inode
    before = snapshot(case.home, case.skill)
    with pytest.raises(ValueError):
        case.service.apply(case.fingerprint)
    assert snapshot(case.home, case.skill) == before


@pytest.mark.parametrize('boundary', ['before-ref', 'after-ref', 'before-receipt'])
def test_reconciliation_interruptions_recover_only_exact_known_git_state(workflow, monkeypatch, boundary):
    from gateway import locked_skill_reconcile
    case = workflow
    case.native(['approve', case.fingerprint])
    case.service.apply(case.fingerprint)
    inode = case.skill.stat().st_ino
    original_git, original_save = locked_skill_reconcile.git, locked_skill_reconcile._save
    def interrupted_git(scope, *args, **kwargs):
        if args[0] == 'update-ref':
            if boundary == 'before-ref':
                raise RuntimeError('synthetic Git interruption')
            result = original_git(scope, *args, **kwargs)
            if boundary == 'after-ref':
                raise RuntimeError('synthetic Git interruption')
            return result
        return original_git(scope, *args, **kwargs)
    def interrupted_save(conn, key, record):
        if boundary == 'before-receipt' and key.startswith('reconciliation:') and record['completed']:
            raise RuntimeError('synthetic Git interruption')
        return original_save(conn, key, record)
    with monkeypatch.context() as scoped:
        scoped.setattr(locked_skill_reconcile, 'git', interrupted_git)
        scoped.setattr(locked_skill_reconcile, '_save', interrupted_save)
        with pytest.raises(RuntimeError, match='synthetic Git interruption'):
            case.service.reconcile(case.fingerprint)
    assert not (case.home / '.git/index.lock').exists()
    assert case.service.reconcile(case.fingerprint)['completed']
    assert case.service.reconcile(case.fingerprint)['already_reconciled']
    assert case.skill.stat().st_ino == inode
    assert git(case.home, 'rev-parse', 'HEAD') == case.manifest['head']


def test_existing_git_lock_is_never_removed_or_bypassed(workflow):
    case = workflow
    case.native(['approve', case.fingerprint])
    case.service.apply(case.fingerprint)
    lock = case.home / '.git/index.lock'
    lock.write_text('owned by a different Git operation\n')
    before = snapshot(case.home, case.skill)
    with pytest.raises(FileExistsError):
        case.service.reconcile(case.fingerprint)
    assert snapshot(case.home, case.skill) == before
    assert lock.read_text() == 'owned by a different Git operation\n'


@pytest.mark.parametrize('symlink', [False, True])
def test_parent_swap_after_intent_refuses_without_writing_new_target(workflow, monkeypatch, symlink):
    from gateway import locked_skill_apply
    case = workflow
    case.native(['approve', case.fingerprint])
    parent = case.skill.parent
    moved = parent.with_name('moved-original')
    replacement = case.home.parent / 'replacement-package'
    replacement.mkdir()
    other_target = replacement / 'SKILL.md'
    other_target.write_bytes(b'unrelated protected fixture\n')
    original_load = locked_skill_apply._load
    count = 0
    def swapped_load(*args):
        nonlocal count
        result = original_load(*args)
        count += 1
        if count == 3:
            parent.rename(moved)
            if symlink:
                parent.symlink_to(replacement, target_is_directory=True)
            else:
                parent.mkdir()
                (parent / 'SKILL.md').write_bytes(b'concurrent replacement directory\n')
        return result
    monkeypatch.setattr(locked_skill_apply, '_load', swapped_load)
    with pytest.raises(ValueError):
        case.service.apply(case.fingerprint)
    assert (moved / 'SKILL.md').read_bytes() == b'---\nname: example\ndescription: Example\n---\nOriginal\n'
    assert other_target.read_bytes() == b'unrelated protected fixture\n'
    if not symlink:
        assert (parent / 'SKILL.md').read_bytes() == b'concurrent replacement directory\n'


@pytest.mark.asyncio
async def test_cancelled_rpc_retains_physical_mutation_and_usage_lock(workflow, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace
    from gateway import locked_skill_apply
    from gateway.locked_skill_endpoints import rpc
    from gateway.session_runtime_workers import mutation_tasks
    from gateway.locked_skill_proposals import _read
    from tools import skill_usage
    case = workflow
    case.native(['approve', case.fingerprint])
    entered, release = Event(), Event()
    original = locked_skill_apply._replace
    def held_replace(*args):
        entered.set()
        assert release.wait(timeout=15)
        return original(*args)
    monkeypatch.setattr(locked_skill_apply, '_replace', held_replace)
    connection = SimpleNamespace(authority=case.service.authority, native_owner=True,
        actor=SimpleNamespace(profile_id=str(case.home), capabilities={'session:operator'}))
    request = asyncio.create_task(rpc(connection, None, {'operation': 'apply', 'digest': case.fingerprint}))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        active = mutation_tasks(case.service.authority)
        assert len(active) == 1
        with (case.home / 'skills/.usage.json.lock').open('a+') as lock:
            with pytest.raises(BlockingIOError):
                skill_usage.fcntl.flock(lock, skill_usage.fcntl.LOCK_EX | skill_usage.fcntl.LOCK_NB)
        with ThreadPoolExecutor(max_workers=1) as workers:
            toggled = workers.submit(skill_usage.set_locked, 'example', False)
            release.set()
            await asyncio.wait_for(asyncio.shield(active[0]), timeout=15)
            assert await asyncio.to_thread(toggled.result, 10)
        assert case.skill.read_bytes() == b'Reviewed exact bytes\n'
        with case.service.db._read_ctx() as conn:
            assert _read(conn, 'application:' + case.fingerprint)['applied']
        assert not skill_usage.is_locked('example')
    finally:
        release.set()
