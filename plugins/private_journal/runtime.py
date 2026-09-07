"""Explicit runtime wiring for the standalone, no-agent batch process."""
import logging
from contextlib import contextmanager
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.config import load_config


def settings():
    cfg = load_config()
    result = (cfg.get('plugins', {}).get('entries', {}).get('private-journal') or {})
    if not isinstance(result, dict):
        raise ValueError('invalid journal configuration')
    return result


@contextmanager
def quiet_transport():
    # Used only in the isolated scheduler process, never in capture/gateway.
    # Do not install tracing integrations or invoke auxiliary lifecycle hooks.
    old = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(old)


def call_model(*, messages, max_tokens, timeout, task):
    cfg = settings()
    model = cfg.get('model')
    provider = cfg.get('provider')
    # One supported credential lane is enough for the installation packet.
    # Explicit OpenRouter model: no auto/default/subscription/provider fallback.
    if provider != 'openrouter' or not isinstance(model, str) or not model.strip():
        raise ValueError('explicit OpenRouter runtime model required')
    if any(word in model.lower() for word in ('claude', 'astra')):
        raise ValueError('journal runtime model is not permitted')
    from agent.auxiliary_client import resolve_provider_client
    with quiet_transport():
        client, resolved = resolve_provider_client(provider=provider, model=model, task=task)
        if client is None or resolved != model:
            raise ValueError('configured model unavailable')
        try:
            # SDK transport retries disabled. Bypass call_llm's automatic paid
            # fallback/auth-repair chain, but reuse its actual credential router.
            return client.with_options(max_retries=0, timeout=timeout).chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens,
                temperature=0, stream=False,
            )
        finally:
            client.close()


def retain_finalized(**kwargs):
    cfg = settings()
    url = cfg.get('hindsight_url')
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        raise ValueError('explicit Hindsight endpoint required')
    # Same SDK used by the existing Hindsight memory provider. No provider
    # initialize(): that would create banks, auto-install, or start a daemon.
    from hindsight_client import Hindsight
    from agent.secret_scope import get_secret
    with quiet_transport():
        client = Hindsight(base_url=url, api_key=get_secret('HINDSIGHT_API_KEY'), timeout=120)
        try:
            return client.retain(**kwargs)
        finally:
            client.close()


def run_batch():
    from .processor import process_pending, _process_lock, _resolve_vault_path
    from .memory import ingest_finalized
    cfg = settings()
    if cfg.get('batch_enabled') is not True:
        raise ValueError('batch is disabled')
    if not cfg.get('model') or not cfg.get('bank_id') or not cfg.get('vault_path') or not cfg.get('schema_root'):
        raise ValueError('activation settings missing')
    process_pending(vault_path=cfg['vault_path'])
    with _process_lock():
        ingest_finalized(_resolve_vault_path(cfg['vault_path']), cfg['bank_id'])
