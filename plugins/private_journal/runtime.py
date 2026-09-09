"""Explicit runtime wiring for the standalone, no-agent batch process."""
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


def settings():
    # Batch-only import: config initialization creates a home skeleton. Raw
    # capture must never trigger it before descriptor-safe storage validation.
    from hermes_cli.config import load_config

    cfg = load_config()
    result = (cfg.get('plugins', {}).get('entries', {}).get('private-journal') or {})
    if not isinstance(result, dict):
        raise ValueError('invalid journal configuration')
    return result


def _settings_from_home_config(home):
    """Read the selected profile through the config owner after safe validation."""
    from . import storage
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    try:
        path = Path(home) / 'config.yaml'
        # Validate before importing general config code, whose dependencies may
        # initialize a home. Missing configs stay OFF without initialization.
        with storage.directory(path.parent):
            if path.is_symlink() or not path.is_file():
                return {}
        token = set_hermes_home_override(home)
        try:
            from hermes_cli.config import load_config_readonly

            data = load_config_readonly()
        finally:
            reset_hermes_home_override(token)
        result = (data.get('plugins', {}).get('entries', {}).get('private-journal') or {})
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def memory_retention_policy(*, home=None):
    cfg = _settings_from_home_config(home) if home is not None else settings()
    enabled = cfg.get('memory_retention_enabled') is True
    provider = cfg.get('memory_provider')
    if not enabled or provider != 'hindsight':
        return {'schema_version': 1, 'enabled': False}
    return {
        'schema_version': 1,
        'enabled': True,
        'provider': provider,
        'bank_id': cfg.get('bank_id') if isinstance(cfg.get('bank_id'), str) else None,
    }


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


def call_model(*, messages, max_tokens, timeout, task, response_format=None, reasoning=None):
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
            request: dict[str, Any] = dict(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0,
                stream=False,
            )
            if response_format is not None:
                request["response_format"] = response_format
            if reasoning is not None:
                request["extra_body"] = {"reasoning": reasoning}
            return client.with_options(max_retries=0, timeout=timeout).chat.completions.create(**request)
        finally:
            client.close()


def retain_memory(*, provider, **kwargs):
    if provider != 'hindsight':
        raise ValueError('unsupported private journal memory provider')
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


def retain_finalized(**kwargs):
    return retain_memory(provider='hindsight', **kwargs)


def run_batch():
    from .processor import process_pending, _process_lock, _resolve_vault_path
    from .memory import ingest_opted_in_raw
    cfg = settings()
    if cfg.get('batch_enabled') is not True:
        raise ValueError('batch is disabled')
    if not cfg.get('model') or not cfg.get('vault_path') or not cfg.get('schema_root'):
        raise ValueError('activation settings missing')
    retention_policy = memory_retention_policy()
    if retention_policy.get('enabled') is True and not cfg.get('bank_id'):
        retention_error = ValueError('memory retention bank missing')
    else:
        retention_error = None
    extraction_error = None
    try:
        process_pending(vault_path=cfg['vault_path'])
    except Exception as exc:
        extraction_error = exc
    with _process_lock():
        if retention_policy.get('enabled') is True and retention_error is None:
            try:
                ingest_opted_in_raw(_resolve_vault_path(cfg['vault_path']), cfg['bank_id'])
            except Exception as exc:
                retention_error = exc
    if extraction_error is not None:
        raise extraction_error
    if retention_error is not None:
        raise retention_error
