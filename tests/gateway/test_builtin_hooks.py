"""Tests for built-in gateway hook registration."""

from gateway.hooks import HookRegistry


def test_builtin_voice_summary_hook_registers_once():
    registry = HookRegistry()

    registry._register_builtin_hooks()
    registry._register_builtin_hooks()

    hooks = [hook for hook in registry.loaded_hooks if hook["name"] == "voice-summary"]
    assert len(hooks) == 1
    assert hooks[0]["events"] == ["agent:end"]


def test_builtin_voice_summary_hook_loads_without_user_hooks_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.hooks.HOOKS_DIR", tmp_path / "missing-hooks")
    registry = HookRegistry()

    registry.discover_and_load()

    assert any(hook["name"] == "voice-summary" for hook in registry.loaded_hooks)
