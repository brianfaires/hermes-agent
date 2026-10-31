"""Serialize pool refreshes at the grant owner while preserving profile-to-root lock order."""
from contextlib import contextmanager


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
