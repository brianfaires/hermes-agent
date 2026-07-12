from types import SimpleNamespace

import pytest

from gateway.kanban_discord_inbox import KanbanReplyInboxConfig, validate_router_config
from gateway.kanban_mirror.config import load_mirror_config
from gateway.platforms.base import Platform
from gateway.run import GatewayRunner, MultiplexConfigError


def cfg(**changes):
    base = dict(enabled=True, conversation_router_enabled=True, board_slug="board",
                forum_channel_ids=frozenset({"forum"}),
                conversation_router_ingress_bot_id="222",
                profile_bot_user_ids=(("111", "default"), ("222", "owner")))
    base.update(changes)
    return KanbanReplyInboxConfig(**base)


def test_router_config_requires_multiplex_scope_and_existing_one_to_one_profiles():
    with pytest.raises(ValueError, match="multiplex_profiles"):
        validate_router_config(cfg(), multiplex_profiles=False, profile_exists_fn=lambda _: True)
    with pytest.raises(ValueError, match="duplicate profiles"):
        validate_router_config(cfg(profile_bot_user_ids=(("111", "owner"), ("222", "owner"))),
                               multiplex_profiles=True, profile_exists_fn=lambda _: True)
    with pytest.raises(ValueError, match="do not exist"):
        validate_router_config(cfg(), multiplex_profiles=True,
                               profile_exists_fn=lambda p: p == "default")


def test_router_config_enforces_ingress_and_mirror_ownership():
    mirror = SimpleNamespace(enabled=True, board="other", forum_channel_id="other-forum")
    with pytest.raises(ValueError, match="must match.*Forums"):
        validate_router_config(cfg(), multiplex_profiles=True,
                               profile_exists_fn=lambda _: True, mirror_config=mirror)
    assert validate_router_config(cfg(), multiplex_profiles=True,
                                  profile_exists_fn=lambda _: True) == "owner"


def test_live_readiness_accepts_secondary_ingress_and_rejects_swapped_ids(monkeypatch):
    monkeypatch.setattr("gateway.kanban_discord_inbox.load_config", cfg)
    monkeypatch.setattr("gateway.kanban_mirror.config.load_mirror_config",
                        lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _: True)
    def adapter(user):
        return SimpleNamespace(_running=True, _client=SimpleNamespace(user=SimpleNamespace(id=user)))
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                             _gateway_profile_name="default",
                             adapters={Platform.DISCORD: adapter("111")},
                             _profile_adapters={"owner": {Platform.DISCORD: adapter("222")}})
    runner._discord_adapter_for_profile = GatewayRunner._discord_adapter_for_profile.__get__(runner)
    runner._kanban_profile_adapters = GatewayRunner._kanban_profile_adapters.__get__(runner)
    validate = GatewayRunner._validate_kanban_router_readiness.__get__(runner)
    assert validate() == "owner"
    assert runner._kanban_router_ingress_profile == "owner"
    assert runner.adapters[Platform.DISCORD]._kanban_router_ingress_identity is None
    assert runner._profile_adapters["owner"][Platform.DISCORD]._kanban_router_ingress_identity == ("owner", "222")
    runner.adapters[Platform.DISCORD]._client.user.id = "222"
    runner._profile_adapters["owner"][Platform.DISCORD]._client.user.id = "111"
    with pytest.raises(MultiplexConfigError, match="does not match"):
        validate()
    assert runner._profile_adapters["owner"][Platform.DISCORD]._kanban_router_ingress_identity is None


def test_daemon_advanced_gates_require_binding_backfill_and_legacy_remains_off():
    assert not load_mirror_config({}).binding_transitions_enabled
    raw = {"kanban": {"discord_mirror": {"reconciliation_enabled": True}}}
    with pytest.raises(ValueError, match="binding_transitions_enabled"):
        load_mirror_config(raw)
    raw["kanban"]["discord_mirror"]["binding_transitions_enabled"] = True
    assert load_mirror_config(raw).reconciliation_enabled
