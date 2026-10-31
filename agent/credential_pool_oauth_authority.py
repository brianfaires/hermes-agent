"""Serialize pool refreshes at the grant owner while preserving profile-to-root lock order."""
from contextlib import contextmanager
from dataclasses import replace
import time


class RejectedPoolGrant(Exception):
    """Retire a rejected grant after releasing the refresh transaction's store locks."""

    def __init__(self, entry, error):
        super().__init__()
        self.entry, self.error = entry, error


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


def refresh_pool_entry(pool, entry, *, force):
    from agent import credential_pool as cp

    try:
        with pool_refresh_transaction(pool.provider, pool._single_use_refresh_lock_timeout()):
            synced = pool._sync_entry_from_pool_store(entry)
            if synced.last_status == cp.STATUS_DEAD or not synced.refresh_token:
                return None
            if pool.provider == 'openai-codex':
                # Independent manual grants rotate only in their pool row. A proven peer
                # generation outranks a legacy singleton that may still hold the spent pair.
                if synced is entry:
                    synced = pool._sync_entry_from_auth_store(entry)
                if (synced is not entry and not pool._entry_needs_refresh(synced)
                        and (not force or synced.access_token != entry.access_token)):
                    return synced
                return pool._refresh_entry_impl(synced, force=force, defer_terminal=True)
            if pool.provider == 'anthropic' and synced.source == 'claude_code':
                # Borrowed rows contain no secrets; the shared Claude file/Keychain is authority.
                with pool._claude_code_credentials_lock():
                    synced = pool._sync_anthropic_entry_from_credentials_file(synced)
                    if synced.refresh_token != entry.refresh_token:
                        return synced
                    return pool._refresh_entry_impl(synced, force=force)
            if synced.access_token != entry.access_token or synced.refresh_token != entry.refresh_token:
                return synced
            return pool._refresh_entry_impl(synced, force=force, defer_terminal=True)
    except RejectedPoolGrant as rejected:
        # A concurrent status writer holds pool -> store; never wait for its pool lock
        # while holding the store lock. Reconcile the grant again under the normal order.
        quarantine_pool_grant(pool, rejected.entry, rejected.error)
        return None


def _quarantine_rows(store, provider, refresh_token, dead):
    pools = store.get('credential_pool')
    rows = pools.get(provider) if isinstance(pools, dict) else None
    affected, removed = set(), set()
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and refresh_token and row.get('refresh_token') == refresh_token:
            affected.add(row['id'])
            if row.get('source') == 'device_code':
                removed.add(row['id'])
            row.pop('access_token', None)
            row.pop('refresh_token', None)
            row.update(dead)
    if removed:
        pools[provider] = [row for row in rows if not isinstance(row, dict) or row.get('id') not in removed]
    return affected, removed


def recover_tokens_refresh(pool, entry, exc, *, defer_terminal):
    """Adopt a peer generation or retire the rejected Codex/xAI grant."""
    from agent import credential_pool as cp

    _, display, _, terminal_fn_name = cp._TOKENS_SINGLETON_PROVIDERS[pool.provider]
    synced = pool._sync_entry_from_pool_store(entry)
    if synced is entry and not (defer_terminal and pool.provider == 'openai-codex'):
        synced = pool._sync_entry_from_auth_store(synced)
    if synced.last_status == cp.STATUS_DEAD:
        return None
    if synced.refresh_token and synced.refresh_token != entry.refresh_token:
        cp.logger.debug('%s OAuth refresh failed but the owner has newer tokens — adopting', display)
        return pool._adopt(synced, **cp._MARK_OK)
    if getattr(cp.auth_mod, terminal_fn_name)(exc):
        cp.logger.warning(
            "%s OAuth refresh token is terminally invalid (%s); clearing local token state. "
            "Re-run 'hermes auth add %s' to sign in again.", display, exc, pool.provider)
        if defer_terminal:
            raise RejectedPoolGrant(entry, exc)
        pool._clear_terminal_tokens_state(entry, exc)
        current = pool._find(lambda item: item.id == entry.id)
        if current is not None and current.refresh_token == entry.refresh_token:
            pool._mark_dead_refresh_grant(current, exc)
        return None
    pool._mark_exhausted(entry, None)
    return None


def quarantine_pool_grant(pool, entry, exc):
    """Retire only the failed owner generation; keep disk and live entries equally unusable."""
    from agent import credential_pool as cp
    from hermes_cli import auth

    with pool._lock, pool_refresh_transaction(pool.provider, pool._single_use_refresh_lock_timeout()):
        source = (cp._borrowed_single_use_pool_root() if not cp._profile_owns_pool_provider(pool.provider)
                  else auth._auth_file_path())
        store = auth._load_auth_store(source)
        typed_error = auth.AuthError(str(exc), provider=pool.provider,
                                     code=getattr(exc, 'code', None) or 'invalid_grant', relogin_required=True)
        error = auth._last_auth_error_marker(pool.provider, typed_error, reason='credential_pool_refresh_failure',
                                             default_code='invalid_grant')
        dead = dict(last_status='dead', last_status_at=time.time(), last_error_code=error['code'],
                    last_error_reason=error['code'], last_error_message=error['message'])
        affected, removed = _quarantine_rows(store, pool.provider, entry.refresh_token, dead)
        changed = bool(affected)
        state = auth._provider_state_in(store, pool.provider)
        tokens = (state or {}).get('tokens')
        same_grant = isinstance(tokens, dict) and (
            bool(entry.refresh_token) and tokens.get('refresh_token') == entry.refresh_token
            or not tokens.get('refresh_token') and bool(tokens.get('access_token')) and tokens['access_token'] == entry.access_token)
        if same_grant:
            state['tokens'] = {key: value for key, value in tokens.items() if key not in ('access_token', 'refresh_token')}
            state['last_auth_error'] = error
            auth._store_provider_state(store, pool.provider, state, set_active=False)
            changed = True
        if changed:
            auth._save_auth_store(store, target_path=source)
        rows = {row['id']: row for row in (store.get('credential_pool') or {}).get(pool.provider, [])
                if isinstance(row, dict) and row.get('id')}
        live = []
        for current in pool._entries:
            if current.id in removed:
                continue
            row = rows.pop(current.id, None)
            if current.id in affected:
                current = cp.PooledCredential.from_dict(pool.provider, row)
            elif row is not None:
                pair, base = auth._credential_token_pair(row), pool._persisted_token_pairs.get(current.id)
                if pair != base and any(pair):
                    current = cp.PooledCredential.from_dict(pool.provider, row)
            elif entry.refresh_token and current.refresh_token == entry.refresh_token:
                current = replace(current, access_token='', refresh_token=None, **dead)
            live.append(current)
        live.extend(cp.PooledCredential.from_dict(pool.provider, row) for key, row in rows.items() if key in affected)
        pool._entries = sorted(live, key=lambda item: item.priority)
        pool._persisted_token_pairs.update(auth._token_pairs_by_id(
            (store.get('credential_pool') or {}).get(pool.provider, [])))
        if pool._current_id in removed:
            pool._current_id = None
