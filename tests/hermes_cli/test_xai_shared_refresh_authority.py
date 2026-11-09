"""Root-borrowed xAI singleton refresh locks the owner and adopts peer rotation."""
import json
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def authority(tmp_path, monkeypatch):
    root = tmp_path / "authority"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    (profile / "auth.json").write_text('{}')
    state = {"auth_mode": "oauth_device_code", "tokens": {"access_token": "fixture-old-at", "refresh_token": "fixture-old-rt"},
             "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"}}
    (root / "auth.json").write_text(json.dumps({"providers": {"xai-oauth": state}}))
    token = set_hermes_home_override(profile)
    try:
        yield root, profile
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('shape', ['singleton', 'pool', 'empty-local', 'local-grant'])
def test_xai_singleton_refresh_locks_root_during_redemption(authority, monkeypatch, shape):
    from hermes_cli import auth
    root, profile = authority
    root_store = json.loads((root / 'auth.json').read_text())
    independent = {'id':'independent','source':'manual:device_code','auth_type':'oauth',
                   'access_token':'fixture-independent-at','refresh_token':'fixture-independent-rt'}
    if shape == 'pool':
        pair = root_store['providers']['xai-oauth']['tokens']
        (root / 'auth.json').write_text(json.dumps({'credential_pool': {'xai-oauth': [
            {'id':'shared','source':'manual:device_code','auth_type':'oauth', **pair}, independent]}}))
        monkeypatch.setattr(auth, '_xai_oauth_discovery', lambda timeout: {'token_endpoint':'https://auth.x.ai/oauth/token'})
    elif shape == 'empty-local':
        (profile / 'auth.json').write_text(json.dumps({'providers': {'xai-oauth': {}}}))
    elif shape == 'local-grant':
        local = json.loads(json.dumps(root_store))
        local['providers']['xai-oauth']['tokens'] = {'access_token':'fixture-local-at','refresh_token':'fixture-local-rt'}
        (profile / 'auth.json').write_text(json.dumps(local))
    observed = []

    def endpoint(access_token, refresh_token, **kwargs):
        observed.append(getattr(auth._auth_lock_holder_for(root / "auth.json"), "depth", 0) > 0)
        return {"access_token": "fixture-new-at", "refresh_token": "fixture-new-rt", "last_refresh": "2026-10-31T00:00:00Z"}

    monkeypatch.setattr(auth, 'refresh_xai_oauth_pure', endpoint)
    result = auth.resolve_xai_oauth_runtime_credentials(force_refresh=True)
    assert result['api_key'] == 'fixture-new-at'
    assert observed == [shape != 'local-grant'], 'refresh must lock the actual grant owner'
    local_state = json.loads((profile / 'auth.json').read_text()).get('providers', {}).get('xai-oauth')
    if shape == 'local-grant':
        assert local_state['tokens']['access_token'] == 'fixture-new-at'
        assert json.loads((root / 'auth.json').read_text()) == root_store
    else:
        assert not local_state or not local_state.get('tokens'), 'borrowers must not create a token copy'
    if shape == 'pool':
        row = json.loads((root / 'auth.json').read_text())['credential_pool']['xai-oauth'][0]
        assert row['refresh_token'] == 'fixture-new-rt', 'pool readers must see the rotated grant'
        assert json.loads((root / 'auth.json').read_text())['credential_pool']['xai-oauth'][1] == independent


def test_xai_refresh_adopts_peer_rotation_without_redeeming_again(authority, monkeypatch):
    from hermes_cli import auth
    root, _profile = authority
    store = json.loads((root / 'auth.json').read_text())
    fresh = {'access_token': 'fixture-peer-at', 'refresh_token': 'fixture-peer-rt'}
    store['providers']['xai-oauth']['tokens'] = fresh
    (root / 'auth.json').write_text(json.dumps(store))
    calls = []

    def endpoint(*args, **kwargs):
        calls.append(True)
        return {**fresh, 'last_refresh': '2026-10-31T00:00:00Z'}

    monkeypatch.setattr(auth, 'refresh_xai_oauth_pure', endpoint)
    result = auth._refresh_xai_oauth_tokens({'access_token':'fixture-old-at','refresh_token':'fixture-old-rt'},
        token_endpoint='https://auth.x.ai/oauth/token', timeout_seconds=2)
    assert result['refresh_token'] == 'fixture-peer-rt'
    assert calls == [], 'a stale caller must adopt the owner generation rather than redeem again'


@pytest.mark.parametrize('pool_backed', [False, True])
def test_terminal_root_refresh_failure_does_not_create_local_shadow(authority, monkeypatch, pool_backed):
    from hermes_cli import auth
    root, profile = authority
    if pool_backed:
        (root / 'auth.json').write_text(json.dumps({'credential_pool': {'xai-oauth': [
            {'id':'shared','source':'manual:device_code','auth_type':'oauth',
             'access_token':'fixture-old-at','refresh_token':'fixture-old-rt'}]}}))
        monkeypatch.setattr(auth, '_xai_oauth_discovery', lambda timeout: {'token_endpoint':'https://auth.x.ai/oauth/token'})

    def reject(*args, **kwargs):
        raise auth.AuthError('synthetic revoked grant', provider='xai-oauth', code='xai_refresh_failed', relogin_required=True)

    monkeypatch.setattr(auth, 'refresh_xai_oauth_pure', reject)
    with pytest.raises(auth.AuthError):
        auth.resolve_xai_oauth_runtime_credentials(force_refresh=True)
    assert not json.loads((profile / 'auth.json').read_text()).get('providers', {}).get('xai-oauth')
    with pytest.raises(auth.AuthError):
        auth.resolve_xai_oauth_runtime_credentials(refresh_if_expiring=False)


def test_fresh_profile_login_does_not_overwrite_root_pool_grant(authority):
    from hermes_cli import auth
    root, profile = authority
    original = {'credential_pool': {'xai-oauth': [
        {'id':'shared','source':'manual:device_code','auth_type':'oauth',
         'access_token':'fixture-old-at','refresh_token':'fixture-old-rt'}]}}
    (root / 'auth.json').write_text(json.dumps(original))
    login = {'access_token':'fixture-independent-at','refresh_token':'fixture-independent-rt'}
    auth._save_xai_oauth_tokens(login)
    assert json.loads((profile / 'auth.json').read_text())['providers']['xai-oauth']['tokens'] == login
    assert json.loads((root / 'auth.json').read_text()) == original


def test_late_quarantine_preserves_newer_peer_generation(authority):
    from hermes_cli import auth
    root, profile = authority
    original = json.loads((root / 'auth.json').read_text())
    original['providers']['xai-oauth']['tokens'] = {'access_token':'fixture-new-at','refresh_token':'fixture-new-rt'}
    (root / 'auth.json').write_text(json.dumps(original))
    from hermes_cli.auth_xai import _quarantine_xai_oauth_tokens
    _quarantine_xai_oauth_tokens(auth.AuthError('synthetic stale failure', provider='xai-oauth',
        code='xai_refresh_failed', relogin_required=True), observed_refresh_token='fixture-old-rt')
    assert json.loads((root / 'auth.json').read_text()) == original
    assert not json.loads((profile / 'auth.json').read_text()).get('providers', {}).get('xai-oauth')
