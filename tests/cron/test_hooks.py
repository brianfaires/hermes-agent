"""Tests for the generic cron lifecycle hook registry (cron/hooks.py)."""

import pytest

from cron import hooks


@pytest.fixture(autouse=True)
def _clean_registry():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


def test_register_and_emit_passes_payload():
    seen = []
    hooks.register_hook(hooks.CREATE, lambda **kw: seen.append(kw))

    hooks.emit(hooks.CREATE, job={"id": "abc"})

    assert seen == [{"job": {"id": "abc"}}]


def test_emit_only_fires_matching_event():
    created, completed = [], []
    hooks.register_hook(hooks.CREATE, lambda **kw: created.append(kw))
    hooks.register_hook(hooks.COMPLETE, lambda **kw: completed.append(kw))

    hooks.emit(hooks.UPDATE, job={"id": "x"})

    assert created == []
    assert completed == []


def test_register_is_idempotent_per_callback():
    calls = []

    def cb(**kw):
        calls.append(kw)

    hooks.register_hook(hooks.REMOVE, cb)
    hooks.register_hook(hooks.REMOVE, cb)
    hooks.emit(hooks.REMOVE, job={"id": "1"})

    assert len(calls) == 1


def test_failing_hook_is_isolated():
    order = []

    def boom(**kw):
        order.append("boom")
        raise RuntimeError("hook failure")

    def ok(**kw):
        order.append("ok")

    hooks.register_hook(hooks.COMPLETE, boom)
    hooks.register_hook(hooks.COMPLETE, ok)

    # Must not raise, and the second hook still runs.
    hooks.emit(hooks.COMPLETE, job={"id": "1"}, success=True)

    assert order == ["boom", "ok"]


def test_unregister_hook():
    calls = []
    cb = lambda **kw: calls.append(kw)
    hooks.register_hook(hooks.CREATE, cb)
    hooks.unregister_hook(hooks.CREATE, cb)

    hooks.emit(hooks.CREATE, job={})

    assert calls == []


def test_register_unknown_event_raises():
    with pytest.raises(ValueError):
        hooks.register_hook("frobnicate", lambda **kw: None)


def test_emit_unknown_event_raises():
    with pytest.raises(ValueError):
        hooks.emit("frobnicate")
