"""Native runtime fallback and agent binding; bootstrap before live-home guards."""
import run_agent
import cli
import gateway.run
from tui_gateway import runtime_fallback, server


class TestResolveRuntimeWithFallback:
    """Tests for _resolve_runtime_with_fallback(): init-time provider
    fallback when the primary provider raises AuthError."""

    def test_primary_success_returns_runtime(self, monkeypatch):
        """When primary resolve succeeds, return its result directly."""
        expected = {"provider": "openai", "api_key": "tok"}
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda **kw: expected,
        )
        resolution = runtime_fallback.resolve_runtime_with_fallback(
            {"requested": "openai"}
        )
        assert resolution.runtime == expected
        assert resolution.selected_model is None
        assert resolution.used_fallback is False

    def test_auth_error_tries_fallback_chain(self, monkeypatch):
        """On AuthError from primary, walk fallback_providers chain."""
        from hermes_cli.auth import AuthError

        fallback_runtime = {"provider": "deepseek", "api_key": "fb-tok"}

        def fake_resolve(**kwargs):
            if kwargs.get("requested") == "openai-codex":
                raise AuthError("No Codex credentials stored")
            return fallback_runtime

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve,
        )
        monkeypatch.setattr(
            server,
            "_load_fallback_model",
            lambda: [{"provider": "deepseek", "model": "deepseek-v4-pro"}],
        )
        resolution = runtime_fallback.resolve_runtime_with_fallback(
            {"requested": "openai-codex"},
        )
        assert resolution.runtime == fallback_runtime
        assert resolution.selected_model == "deepseek-v4-pro"
        assert resolution.used_fallback is True


    def test_auth_error_skips_provider_only_fallback(self, monkeypatch):
        """Auth fallback requires one complete provider/model pair."""
        from hermes_cli.auth import AuthError

        requested = []
        fallback_runtime = {"provider": "openrouter", "api_key": "fb-tok"}

        def fake_resolve(**kwargs):
            requested.append(kwargs.get("requested"))
            if kwargs.get("requested") == "openai-codex":
                raise AuthError("No Codex credentials stored")
            return fallback_runtime

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve,
        )
        monkeypatch.setattr(
            server,
            "_load_fallback_model",
            lambda: [
                {"provider": "anthropic"},
                {"provider": "openrouter", "model": "z-ai/glm-5.2"},
            ],
        )

        resolution = runtime_fallback.resolve_runtime_with_fallback(
            {"requested": "openai-codex"}
        )

        assert requested == ["openai-codex", "openrouter"]
        assert resolution.runtime == fallback_runtime
        assert resolution.selected_model == "z-ai/glm-5.2"
        assert resolution.used_fallback is True

    def test_fallback_entry_key_env_resolves_api_key(self, monkeypatch):
        """A fallback entry naming its key via key_env passes the resolved
        env value as explicit_api_key (#43861, @VrtxOmega)."""
        from hermes_cli.auth import AuthError

        monkeypatch.setenv("FB_TEST_KEY", "env-resolved-key")
        captured = {}
        fallback_runtime = {"provider": "openrouter", "api_key": "x"}

        def fake_resolve(**kwargs):
            if kwargs.get("requested") == "openai-codex":
                raise AuthError("No Codex credentials stored")
            captured.update(kwargs)
            return fallback_runtime

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve,
        )
        monkeypatch.setattr(
            server,
            "_load_fallback_model",
            lambda: [
                {
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.2",
                    "key_env": "FB_TEST_KEY",
                }
            ],
        )
        resolution = runtime_fallback.resolve_runtime_with_fallback(
            {"requested": "openai-codex"}
        )
        assert resolution.used_fallback is True
        assert captured.get("explicit_api_key") == "env-resolved-key"

    def test_auth_error_all_fallbacks_fail_raises(self, monkeypatch):
        """When all fallbacks also fail, re-raise the original AuthError."""
        from hermes_cli.auth import AuthError

        def fake_resolve(**kwargs):
            raise AuthError("No credentials for " + str(kwargs.get("requested")))

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve,
        )
        monkeypatch.setattr(
            server,
            "_load_fallback_model",
            lambda: [{"provider": "deepseek", "model": "deepseek-v4-pro"}],
        )
        import pytest

        with pytest.raises(AuthError, match="No credentials for openai-codex"):
            runtime_fallback.resolve_runtime_with_fallback(
                {"requested": "openai-codex"},
            )

    def test_auth_error_skips_non_dict_entries(self, monkeypatch):
        """Fallback chain entries that are not dicts are skipped."""
        from hermes_cli.auth import AuthError

        fallback_runtime = {"provider": "anthropic", "api_key": "ant-tok"}

        def fake_resolve(**kwargs):
            if kwargs.get("requested") == "openai-codex":
                raise AuthError("No Codex credentials stored")
            return fallback_runtime

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve,
        )
        monkeypatch.setattr(
            server,
            "_load_fallback_model",
            lambda: [
                "invalid-string-entry",
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            ],
        )
        resolution = runtime_fallback.resolve_runtime_with_fallback(
            {"requested": "openai-codex"},
        )
        assert resolution.runtime == fallback_runtime
        assert resolution.selected_model == "claude-sonnet-4-6"
        assert resolution.used_fallback is True

    def test_make_agent_uses_fallback_on_auth_error(self, monkeypatch):
        """Integration: _make_agent falls back to configured fallback
        provider when the primary provider raises AuthError."""
        import types

        from hermes_cli.auth import AuthError

        captured = {}
        fallback_runtime = {
            "provider": "deepseek",
            "api_key": "fb-tok",
            "base_url": "https://fallback.invalid/v1",
        }

        def fake_resolve(**kwargs):
            if kwargs.get("requested") in (None, "openai-codex"):
                raise AuthError("No Codex credentials stored")
            return fallback_runtime

        def fake_agent(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(model=kwargs.get("model"))

        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
        monkeypatch.setattr(
            server,
            "_load_cfg",
            lambda: {
                "model": {"default": "gpt-5.5", "provider": "openai-codex"},
                "fallback_providers": [
                    {"provider": "deepseek", "model": "deepseek-v4-pro"},
                ],
            },
        )
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve,
        )
        monkeypatch.setattr("run_agent.AIAgent", fake_agent)
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *_a, **_kw: ["file"])
        monkeypatch.setattr(server, "_get_db", lambda: None)

        agent = server._make_agent(
            "sid",
            "session-key",
            model_override={
                "model": "gpt-5.5",
                "provider": "openai-codex",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_key": "stale-codex-token",
            },
        )

        assert agent.model == "deepseek-v4-pro"
        assert captured["provider"] == "deepseek"
        assert captured["base_url"] == "https://fallback.invalid/v1"
        assert captured["api_key"] == "fb-tok"
