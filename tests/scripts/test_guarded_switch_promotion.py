"""Promotion contracts with real disposable Git/remotes and the existing fake service."""
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('guard_fixture', Path(__file__).with_name('test_guarded_switch.py'))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@pytest.fixture
def promotion():
    t = harness.GuardTests()
    t.setUp()
    try:
        t.git('branch', 'main', t.m['known_good']['sha'])
        remote = t.root / 'remote.git'
        t.git('init', '--bare', str(remote))
        t.git('remote', 'add', 'origin', str(remote))
        t.git('push', 'origin', 'main')
        t.git('switch', 'target')
        candidate = t.m['target']
        t.m.update(target={**candidate, 'branch': 'main'}, current=candidate)
        receipt = t.install / 'pass.json'
        harness.write_json(receipt, {'verdict': 'PASS', 'source': candidate,
                                    'repo': str(t.repo), 'service': t.m['service'],
                                    'invocation': harness.BASE, 'interpreter': t.m['interpreter']})
        t.m['promotion'] = {
            'old_main': {**t.m['known_good'], 'branch': 'main'},
            'live_pass': {'path': str(receipt), 'sha256': t.module.digest(receipt.read_bytes())},
            'publication': {'remote': 'origin', 'url': str(remote), 'ref': 'refs/heads/main',
                            'old': t.m['known_good']['sha'], 'new': candidate['sha'],
                            'tracking_ref': 'refs/remotes/origin/main', 'route': 'disposable-local'}}
        t.freeze()
        yield t
    finally:
        t.doCleanups()


def execute(t, mode='run'):
    if mode == 'recover':
        t.configure(fenced=True)
    return t.module.execute(mode, str(t.manifest), t.sha, harness.FixtureService)


def remote_tip(t):
    return t.git('ls-remote', 'origin', 'refs/heads/main').split()[0]


def test_normal_promotion(promotion):
    t = promotion
    assert execute(t) == 'switched'
    assert t.git('rev-parse', 'main') == remote_tip(t) == t.m['target']['sha']
    assert t.git('rev-parse', 'origin/main') == remote_tip(t)
    assert t.git('rev-parse', 'good') == t.m['known_good']['sha']
    assert t.service_data()['actions'] == ['stop', 'start']
    assert t.service_data()['smoke_at_start'] == ['target']
    assert t.transaction()['publication_status'] == 'new'
    assert execute(t, 'recover') == 'switched'
    with pytest.raises(t.module.Refused, match='single-use'):
        execute(t)


@pytest.mark.parametrize('fault', ['missing_pass', 'wrong_pass', 'old', 'new', 'ref', 'non_ff'])
def test_preflight_never_stops(promotion, fault):
    t = promotion
    p = t.m['promotion']
    if fault == 'missing_pass':
        Path(p['live_pass']['path']).unlink()
    elif fault == 'wrong_pass':
        data = json.loads(Path(p['live_pass']['path']).read_text(encoding='utf-8'))
        data['invocation'] = 'd' * 32
        harness.write_json(Path(p['live_pass']['path']), data)
        p['live_pass']['sha256'] = t.module.digest(Path(p['live_pass']['path']).read_bytes())
    elif fault == 'non_ff':
        t.git('switch', 'good')
        (t.repo / 'app.py').write_text("VALUE = 'unrelated'\n", encoding="utf-8")
        t.git('commit', '-am', 'divergent')
        t.m['known_good'] = t.spec('good')
        p['old_main'] = {**t.m['known_good'], 'branch': 'main'}
        p['publication']['old'] = t.m['known_good']['sha']
        t.git('branch', '-f', 'main', p['publication']['old'])
        t.git('switch', 'target')
    else:
        p['publication'][fault] = 'refs/heads/other' if fault == 'ref' else '0' * 40
    t.freeze()
    with pytest.raises((t.module.Refused, FileNotFoundError)):
        execute(t)
    assert t.service_data()['actions'] == []
    assert not (t.state / 'transaction.json').exists()


class Death(BaseException):
    pass


@pytest.mark.parametrize('after', [False, True])
def test_crash_at_ff_boundary_restores_without_rewind(promotion, after):
    t = promotion
    original = t.module.git

    def interrupt(repo, *args):
        if args[0] == 'merge':
            assert t.transaction()['ff_intent']
            if after:
                original(repo, *args)
            raise Death()
        return original(repo, *args)

    with patch.object(t.module, 'git', interrupt), pytest.raises(Death):
        execute(t)
    assert execute(t, 'recover') == 'recovered'
    assert t.git('rev-parse', 'main') == t.m['target' if after else 'known_good']['sha']
    t.assert_good()
    assert remote_tip(t) == t.m['known_good']['sha']


@pytest.mark.parametrize('fault', ['failure', 'timeout_old', 'timeout_new', 'unknown', 'death_new'])
def test_publication_failure_or_interruption_recovers(promotion, fault):
    t = promotion
    original = t.module.publication_git
    attempts = []

    def publication(m, verb):
        if verb == 'push':
            attempts.append(verb)
            assert t.transaction()['publication_intent'] == t.m['promotion']['publication']
            if fault in ('timeout_new', 'death_new'):
                original(m, verb)
            if fault == 'death_new':
                raise Death()
            if fault == 'failure':
                raise t.module.Refused('injected publication failure')
            raise subprocess.TimeoutExpired(['git'], 60)
        if fault == 'unknown' and attempts:
            raise t.module.Refused('readback unavailable')
        return original(m, verb)

    with patch.object(t.module, 'publication_git', publication):
        if fault == 'death_new':
            with pytest.raises(Death):
                execute(t)
        else:
            outcome = execute(t)
            assert outcome == ('recovered' if fault == 'failure' else 'awaiting_fence')
        if fault != 'failure':
            assert t.service_data()['actions'] == ['stop']
            assert execute(t, 'recover') == 'recovered'
    assert attempts == ['push']
    t.assert_good()
    assert t.git('rev-parse', 'main') == t.m['target']['sha']
    expected = 'unknown' if fault == 'unknown' else ('new' if fault.endswith('new') else 'old')
    assert t.transaction()['publication_status'] == expected
    assert t.git('rev-parse', 'origin/main') == t.m['target' if expected == 'new' else 'known_good']['sha']


@pytest.mark.parametrize('tip', ['old', 'new', 'unrelated'])
def test_remote_preconditions(promotion, tip):
    t = promotion
    if tip == 'new':
        t.git('push', 'origin', t.m['target']['sha'] + ':refs/heads/main')
        t.git('update-ref', 'refs/remotes/origin/main', t.m['known_good']['sha'])
    elif tip == 'unrelated':
        t.git('switch', 'good')
        (t.repo / 'app.py').write_text("VALUE = 'unrelated'\n", encoding="utf-8")
        t.git('commit', '-am', 'remote divergence')
        t.git('push', 'origin', 'HEAD:main')
        t.git('switch', 'target')
        t.git('branch', '-f', 'good', t.m['known_good']['sha'])
        t.git('update-ref', 'refs/remotes/origin/main', t.m['known_good']['sha'])
    if tip == 'unrelated':
        with pytest.raises(t.module.Refused):
            execute(t)
        assert t.service_data()['actions'] == []
    else:
        original = t.module.publication_git
        pushes = []

        def observe(m, verb):
            if verb == 'push':
                pushes.append(verb)
            return original(m, verb)

        with patch.object(t.module, 'publication_git', observe):
            assert execute(t) == 'switched'
        assert len(pushes) == (tip == 'old')


@pytest.mark.parametrize('fault', ['dirty', 'ref', 'new_invocation'])
def test_recovery_refuses_undeclared_state(promotion, fault):
    t = promotion
    original = t.module.git

    def interrupt(repo, *args):
        result = original(repo, *args)
        if args[0] == 'merge':
            raise Death()
        return result

    with patch.object(t.module, 'git', interrupt), pytest.raises(Death):
        execute(t)
    if fault == 'dirty':
        (t.repo / 'app.py').write_text('dirty\n', encoding='utf-8')
    elif fault == 'ref':
        t.git('branch', 'unrelated')
    else:
        t.configure(gateway={'active': 'active', 'pid': 999, 'invocation': 'd' * 32})
    with pytest.raises(t.module.Refused):
        execute(t, 'recover')
    assert t.service_data()['actions'] == ['stop']


def test_recovery_does_not_need_remote_or_pass_file(promotion):
    t = promotion
    original = t.module.publication_git

    def interrupted(m, verb):
        if verb == 'push':
            original(m, verb)
            raise Death()
        return original(m, verb)

    with patch.object(t.module, 'publication_git', interrupted), pytest.raises(Death):
        execute(t)
    Path(t.m['promotion']['publication']['url']).rename(t.root / 'unavailable.git')
    Path(t.m['promotion']['live_pass']['path']).unlink()
    assert execute(t, 'recover') == 'recovered'
    t.assert_good()
    assert t.git('rev-parse', 'main') == t.m['target']['sha']
    assert t.transaction()['publication_status'] == 'unknown'


@pytest.mark.parametrize('fault', ['origin', 'tracking_mapping', 'credential', 'partial_clone', 'pass_digest'])
def test_unsafe_route_and_evidence_refused_before_stop(promotion, fault):
    t = promotion
    if fault == 'origin':
        t.git('remote', 'set-url', 'origin', str(t.root / 'other.git'))
    elif fault == 'tracking_mapping':
        t.git('config', 'remote.origin.fetch', '+refs/heads/*:refs/heads/*')
    elif fault == 'credential':
        t.git('config', 'credential.helper', 'must-not-run')
    elif fault == 'partial_clone':
        t.git('config', 'remote.origin.promisor', 'true')
    else:
        Path(t.m['promotion']['live_pass']['path']).write_text('{}', encoding='utf-8')
    with pytest.raises(t.module.Refused):
        execute(t)
    assert t.service_data()['actions'] == []
    assert not (t.state / 'transaction.json').exists()


def test_approved_credential_route_is_publication_only(promotion):
    t = promotion
    p = t.m['promotion']['publication']
    p.update(route='brianfaires-gh', url=t.module.APPROVED_URL)
    calls = []

    def capture(argv, cwd, environ=None):
        calls.append((argv, environ or t.module.env()))
        return ''

    # Observe argv/environment only. Never execute gh or contact GitHub.
    with patch.object(t.module, 'command', capture):
        t.module.git(t.repo, 'rev-parse', 'HEAD')
        t.module.publication_git(t.m, 'push')
    local, public = calls
    assert local[1]['HOME'] == '/nonexistent'
    assert not any('git-credential' in a for a in local[0])
    assert public[1]['HOME'] == '/home/brian'
    assert public[1]['GIT_CONFIG_GLOBAL'] == '/dev/null'
    assert 'credential.https://github.com.helper=/usr/bin/gh auth git-credential' in public[0]
    assert 'credential.https://github.com.username=brianfaires' in public[0]
    assert public[0][-2:] == [p['url'], p['new'] + ':' + p['ref']]
    assert not any(a.startswith('--force') or a.startswith('+') for a in public[0])


@pytest.mark.parametrize('fault', ['early_main', 'early_tracking', 'good_branch'])
def test_no_declared_ref_changes_before_intent(promotion, fault):
    t = promotion
    original = t.module.Transaction.save

    def interrupt(tx, phase, **values):
        original(tx, phase, **values)
        if phase == 'stopped':
            raise Death()

    with patch.object(t.module.Transaction, 'save', interrupt), pytest.raises(Death):
        execute(t)
    ref = {'early_main': 'refs/heads/main', 'early_tracking': 'refs/remotes/origin/main',
           'good_branch': 'refs/heads/good'}[fault]
    t.git('update-ref', ref, t.m['target']['sha'])
    with pytest.raises(t.module.Refused, match='ref drift'):
        execute(t, 'recover')
    assert t.service_data()['actions'] == ['stop']


def test_unrelated_remote_after_arm_restores_without_push(promotion):
    t = promotion
    original = t.module.publication_git
    calls = []

    def changed(m, verb):
        calls.append(verb)
        if (t.state / 'transaction.json').exists():
            assert verb != 'push'
            return '0' * 40 + '\trefs/heads/main\n'
        return original(m, verb)

    with patch.object(t.module, 'publication_git', changed):
        assert execute(t) == 'recovered'
    t.assert_good()
    assert t.transaction()['publication_status'] == 'unrelated'
    assert 'push' not in calls
    assert remote_tip(t) == t.m['known_good']['sha']
