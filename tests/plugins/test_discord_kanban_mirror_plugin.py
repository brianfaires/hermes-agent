from pathlib import Path


def test_discord_kanban_mirror_is_owned_by_discord_plugin() -> None:
    from gateway.run import GatewayRunner
    from plugins.platforms.discord.kanban_mirror.runtime import (
        DiscordKanbanMirrorRuntimeMixin,
    )

    assert issubclass(GatewayRunner, DiscordKanbanMirrorRuntimeMixin)
    assert not Path("gateway/kanban_mirror").exists()
    assert not Path("gateway/kanban_discord_inbox.py").exists()
