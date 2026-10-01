"""Execution-local inference restrictions; never changes profile configuration."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature


_subscription_only = ContextVar("subscription_only", default=False)
_route = ContextVar("subscription_route", default=(None, None))


def subscription_only_active():
    return _subscription_only.get()


def validate_subscription_only(value, provider=None, model=None, base_url=None):
    if type(value) is not bool:
        raise ValueError("subscription_only must be a boolean")
    if value and (provider != "openai-codex" or not isinstance(model, str) or not model.strip() or
                  (base_url and base_url.rstrip("/") != "https://chatgpt.com/backend-api/codex")):
        raise ValueError(
            "subscription_only requires explicit openai-codex provider and model, "
            "with no custom base_url"
        )
    return value


@contextmanager
def inference_scope(enabled=False):
    if type(enabled) is not bool:
        raise ValueError("subscription_only must be a boolean")
    token = _subscription_only.set(enabled or subscription_only_active())
    try:
        yield
    finally:
        _subscription_only.reset(token)


def scoped_inference(fn):
    """Bind a job/CLI/agent policy, restoring the caller's context on exit."""
    sig = signature(fn)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        bound = sig.bind(*args, **kwargs).arguments
        owner = bound.get("self")
        job = bound.get("job") or {}
        enabled = bound.get("subscription_only", job.get("subscription_only", False))
        if type(enabled) is not bool:
            raise ValueError("subscription_only must be a boolean")
        if owner is not None:
            enabled = enabled or getattr(owner, "subscription_only", False)
        with inference_scope(enabled):
            if owner is not None and fn.__name__ == "__init__":
                owner.subscription_only = subscription_only_active()
            provider = bound.get("provider") or job.get("provider") or getattr(owner, "provider", None)
            model = bound.get("model") or job.get("model") or getattr(owner, "model", None)
            token = _route.set((provider, model) if provider and model else _route.get())
            try:
                return fn(*args, **kwargs)
            finally:
                _route.reset(token)
    return wrapped


def reject_auxiliary_inference():
    if subscription_only_active():
        raise RuntimeError("subscription_only prohibits auxiliary inference")


def inherit_subscription_policy(enabled, provider, model, base_url=None):
    if type(enabled) is not bool:
        raise ValueError("subscription_only must be a boolean")
    if subscription_only_active():
        enabled = True
        inherited_provider, inherited_model = _route.get()
        provider = provider or inherited_provider
        model = model or inherited_model
    validate_subscription_only(enabled, provider, model, base_url)
    return enabled, provider, model
