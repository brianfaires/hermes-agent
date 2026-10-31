"""A borrowed rotating pool grant must be locked at its actual owner during redemption."""
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


def _refresh_worker(root, provider, name):
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
        return {'access_token':'fixture-peer-access','refresh_token':'fixture-peer-refresh',
                'last_refresh':'2026-10-31T00:00:00Z'}

    setattr(auth, cp._TOKENS_SINGLETON_PROVIDERS[provider][2], endpoint)
    pool = cp.load_pool(provider)
    stale = pool.entries()[0]
    (root / (name + '.ready')).write_text('ready')
    assert wait_until(lambda: all((root / (n + '.ready')).exists() for n in ('a', 'b')), timeout=20)
    result = pool._refresh_entry(stale, force=True)
    assert result is not None and result.refresh_token == 'fixture-peer-refresh'


@pytest.mark.parametrize('provider', ['openai-codex', 'xai-oauth'])
def test_distinct_profile_processes_redeem_shared_grant_once(tmp_path, provider):
    root = tmp_path / 'authority'
    for name in ('a', 'b'):
        home = root / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'auth.json').write_text('{}')
        (home / 'config.yaml').write_text(json.dumps({'auth': {'adopt_external_logins': False}}))
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {provider: [
        {'id':'shared','source':'manual:device_code','auth_type':'oauth','priority':0,
         'access_token':'fixture-old-access','refresh_token':'fixture-old-refresh'}]}}))
    (root / 'fake-server.json').write_text('[]')
    workers = [subprocess.Popen([sys.executable, '-m', 'tests.agent.test_shared_oauth_refresh_authority',
                                 str(root), provider, name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


if __name__ == '__main__':
    _refresh_worker(*sys.argv[1:])
