"""Serialize pool refreshes at the grant owner while preserving profile-to-root lock order."""
from contextlib import contextmanager
from dataclasses import replace
import time


@contextmanager
def pool_refresh_transaction(provider: str, timeout: float):
    from agent import credential_pool as cp
    from hermes_cli import auth

    with auth._auth_store_lock(timeout_seconds=timeout):
        root = (cp._borrowed_single_use_pool_root()
                if provider in cp.SINGLE_USE_REFRESH_POOL_PROVIDERS and not cp._profile_owns_pool_provider(provider)
                else None)
        with auth._auth_store_lock(timeout_seconds=timeout, target_path=root):
            yield


def _quarantine_rows(store, provider, refresh_token, dead):
    pools = store.get('credential_pool')
    rows = pools.get(provider) if isinstance(pools, dict) else None
    changed = False
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and refresh_token and row.get('refresh_token') == refresh_token:
            row.pop('access_token', None)
            row.pop('refresh_token', None)
            row.update(dead)
            changed = True
    return changed


def quarantine_pool_grant(pool, entry, exc):
    """Retire only the failed owner generation; keep disk and live entries equally unusable."""
    from agent import credential_pool as cp
    from hermes_cli import auth

    with pool_refresh_transaction(pool.provider, pool._single_use_refresh_lock_timeout()):
        source = (cp._borrowed_single_use_pool_root() if not cp._profile_owns_pool_provider(pool.provider)
                  else auth._auth_file_path())
        store = auth._load_auth_store(source)
        error = auth._last_auth_error_marker(pool.provider, exc, reason='credential_pool_refresh_failure',
                                             default_code='invalid_grant')
        dead = dict(last_status='dead', last_status_at=time.time(), last_error_code=error['code'],
                    last_error_reason=error['code'], last_error_message=error['message'])
        changed = _quarantine_rows(store, pool.provider, entry.refresh_token, dead)
        state = auth._provider_state_in(store, pool.provider)
        tokens = (state or {}).get('tokens')
        if isinstance(tokens, dict) and entry.refresh_token and tokens.get('refresh_token') == entry.refresh_token:
            state['tokens'] = {key: value for key, value in tokens.items() if key not in ('access_token', 'refresh_token')}
            state['last_auth_error'] = error
            auth._store_provider_state(store, pool.provider, state, set_active=False)
            changed = True
        if changed:
            auth._save_auth_store(store, target_path=source)
        for current in pool.entries():
            if entry.refresh_token and current.refresh_token == entry.refresh_token:
                pool._replace_entry(current, replace(current, access_token='', refresh_token=None, **dead))
