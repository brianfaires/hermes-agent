"""A borrowed rotating pool grant must be locked at its actual owner during redemption."""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
@pytest.mark.parametrize("borrowed", [False, True], ids=["root", "profile"])
def test_pool_refresh_locks_owning_store_during_redemption(tmp_path, monkeypatch, provider, borrowed):
    from agent import credential_pool as cp
    from hermes_cli import auth

    root = tmp_path / "authority"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    row = {"id": "shared", "source": "manual:device_code", "auth_type": "oauth", "priority": 0,
           "access_token": "fixture-old-access", "refresh_token": "fixture-old-refresh"}
    (root / "auth.json").write_text(json.dumps({"credential_pool": {provider: [row]}}))
    (profile / "auth.json").write_text(json.dumps({"credential_pool": {}}))
    observed = []

    def endpoint(access_token, refresh_token, **kwargs):
        observed.append(getattr(auth._auth_lock_holder_for(root / "auth.json"), "depth", 0) > 0)
        return {"access_token": "fixture-new-access", "refresh_token": "fixture-new-refresh",
                "last_refresh": "2026-10-31T00:00:00Z"}

    monkeypatch.setattr(auth, cp._TOKENS_SINGLETON_PROVIDERS[provider][2], endpoint)
    token = set_hermes_home_override(profile if borrowed else root)
    try:
        pool = cp.load_pool(provider)
        result = pool._refresh_entry(pool.entries()[0], force=True)
    finally:
        reset_hermes_home_override(token)
    assert result is not None
    assert observed == [True], "the owner lock must cover token redemption, not only persistence"
    assert json.loads((root / "auth.json").read_text())["credential_pool"][provider][0]["refresh_token"] == "fixture-new-refresh"
    assert not json.loads((profile / "auth.json").read_text()).get("credential_pool", {}).get(provider)


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return f'eyJhbGciOiJub25lIn0.{payload}.fixture'


def _refresh_worker(root, provider, name, refresh_kind):
    from agent import credential_pool as cp
    from hermes_cli import auth
    from tests.waiting import wait_until
    root = Path(root)
    home = root / 'profiles' / name
    os.environ['HERMES_HOME'] = str(home)
    server = root / 'fake-server.json'

    def endpoint(access_token, refresh_token, **kwargs):
        with auth._auth_store_lock(target_path=server):
            state = json.loads(server.read_text())
            state.append({'presented': refresh_token,
                          'owner_locked': getattr(auth._auth_lock_holder_for(root / 'auth.json'), 'depth', 0) > 0})
            server.write_text(json.dumps(state))
        return {'access_token':_jwt(4102444800),'refresh_token':'fixture-peer-refresh',
                'last_refresh':'2026-10-31T00:00:00Z'}

    setattr(auth, cp._TOKENS_SINGLETON_PROVIDERS[provider][2], endpoint)
    pool = cp.load_pool(provider)
    stale = pool.entries()[0]
    (root / (name + '.ready')).write_text('ready')
    assert wait_until(lambda: all((root / (n + '.ready')).exists() for n in ('a', 'b')), timeout=20)
    result = pool.select() if refresh_kind == 'selection' else pool._refresh_entry(stale, force=True)
    assert result is not None and result.refresh_token == 'fixture-peer-refresh'


@pytest.mark.parametrize('provider', ['openai-codex', 'xai-oauth'])
@pytest.mark.parametrize('refresh_kind', ['selection', 'forced'])
def test_distinct_profile_processes_redeem_shared_grant_once(tmp_path, provider, refresh_kind):
    root = tmp_path / 'authority'
    for name in ('a', 'b'):
        home = root / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'auth.json').write_text('{}')
        (home / 'config.yaml').write_text(json.dumps({'auth': {'adopt_external_logins': False}}))
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {provider: [
        {'id':'shared','source':'manual:device_code','auth_type':'oauth','priority':0,
         'access_token':_jwt(1),'refresh_token':'fixture-old-refresh'}]}}))
    (root / 'fake-server.json').write_text('[]')
    workers = [subprocess.Popen([sys.executable, '-m', 'tests.agent.test_shared_oauth_refresh_authority',
                                 str(root), provider, name, refresh_kind], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for name in ('a', 'b')]
    try:
        for process in workers:
            stdout, stderr = process.communicate(timeout=40)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.wait()
    calls = json.loads((root / 'fake-server.json').read_text())
    assert calls == [{'presented':'fixture-old-refresh', 'owner_locked':True}]
    for name in ('a', 'b'):
        assert not json.loads((root / 'profiles' / name / 'auth.json').read_text()).get('credential_pool', {}).get(provider)


@pytest.mark.parametrize('provider', ['openai-codex', 'xai-oauth'])
@pytest.mark.parametrize('has_other', [False, True])
def test_terminal_borrowed_pool_failure_cannot_resurrect_through_singleton(tmp_path, monkeypatch, provider, has_other):
    from agent import credential_pool as cp
    from hermes_cli import auth
    root = tmp_path / 'authority'
    profile = root / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    (profile / 'auth.json').write_text('{}')
    rows = [
        {'id':'shared','source':'manual:device_code','auth_type':'oauth',
         'access_token':'fixture-revoked-at','refresh_token':'fixture-revoked-rt'}]
    if has_other:
        rows.append({'id':'independent','source':'manual:device_code','auth_type':'oauth','priority':1,
                     'access_token':'fixture-healthy-at','refresh_token':'fixture-healthy-rt'})
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {provider: rows}}))

    def reject(*args, **kwargs):
        code = 'codex_refresh_failed' if provider == 'openai-codex' else 'xai_refresh_failed'
        raise auth.AuthError('synthetic invalid_grant', provider=provider, code=code, relogin_required=True)

    monkeypatch.setattr(auth, cp._TOKENS_SINGLETON_PROVIDERS[provider][2], reject)
    token = set_hermes_home_override(profile)
    try:
        pool = cp.load_pool(provider)
        assert pool._refresh_entry(pool.entries()[0], force=True) is None
        assert not json.loads((profile / 'auth.json').read_text()).get('providers', {}).get(provider)
        row = json.loads((root / 'auth.json').read_text())['credential_pool'][provider][0]
        assert not row.get('access_token') and not row.get('refresh_token')
        assert row['last_status'] == 'dead'
        # Both the active pool and a new singleton resolver must reject the quarantined grant.
        resolver = auth.resolve_codex_runtime_credentials if provider == 'openai-codex' else auth.resolve_xai_oauth_runtime_credentials
        if has_other:
            assert pool.peek().access_token == 'fixture-healthy-at'
            assert resolver(refresh_if_expiring=False)['api_key'] == 'fixture-healthy-at'
        else:
            assert not pool.peek()
            with pytest.raises(auth.AuthError):
                resolver(refresh_if_expiring=False)
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('provider', ['openai-codex', 'xai-oauth'])
def test_preloaded_peer_cannot_restore_quarantined_generation(tmp_path, monkeypatch, provider):
    from agent import credential_pool as cp
    from hermes_cli import auth
    root = tmp_path / 'authority'
    for name in ('a', 'b'):
        home = root / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'auth.json').write_text('{}')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {provider: [
        {'id':'shared','source':'manual:device_code','auth_type':'oauth',
         'access_token':'fixture-stale-at','refresh_token':'fixture-stale-rt'}]}}))
    pools = []
    for name in ('a', 'b'):
        token = set_hermes_home_override(root / 'profiles' / name)
        try:
            pools.append(cp.load_pool(provider))
        finally:
            reset_hermes_home_override(token)

    def reject(*args, **kwargs):
        code = 'codex_refresh_failed' if provider == 'openai-codex' else 'xai_refresh_failed'
        raise auth.AuthError('synthetic revoked grant', provider=provider, code=code, relogin_required=True)

    monkeypatch.setattr(auth, cp._TOKENS_SINGLETON_PROVIDERS[provider][2], reject)
    token = set_hermes_home_override(root / 'profiles' / 'a')
    try:
        assert pools[0]._refresh_entry(pools[0].entries()[0], force=True) is None
    finally:
        reset_hermes_home_override(token)
    calls = []

    def transient(*args, **kwargs):
        calls.append(True)
        raise TimeoutError('synthetic transient endpoint failure')

    monkeypatch.setattr(auth, cp._TOKENS_SINGLETON_PROVIDERS[provider][2], transient)
    token = set_hermes_home_override(root / 'profiles' / 'b')
    try:
        assert pools[1]._refresh_entry(pools[1].entries()[0], force=True) is None
    finally:
        reset_hermes_home_override(token)
    assert calls == [], 'a peer must adopt the terminal owner state before contacting the provider'
    row = json.loads((root / 'auth.json').read_text())['credential_pool'][provider][0]
    assert row['last_status'] == 'dead' and not row.get('access_token') and not row.get('refresh_token')


if __name__ == '__main__':
    _refresh_worker(*sys.argv[1:])
