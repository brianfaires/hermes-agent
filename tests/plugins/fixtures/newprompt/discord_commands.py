"""Expose the first prompt on Discord's native /new command using the SDK handler API."""
import asyncio
from pathlib import Path


def register_discord(ctx, instance):
    owner = Path(ctx._manager.home_path)

    def wire(native, adapter):
        import discord

        tree = native.tree
        previous = tree.get_command('new')
        if previous is None:
            return  # Native slash commands were disabled.
        loop = asyncio.get_running_loop()

        @discord.app_commands.command(name='new', description=previous.description)
        @discord.app_commands.describe(prompt='Optional first prompt for the new session')
        async def new_session(interaction: discord.Interaction, prompt: str = ''):
            from gateway.run import _async_profile_runtime_scope
            async with _async_profile_runtime_scope(owner):
                await adapter._run_simple_slash(interaction, ('/new ' + prompt).strip(),
                    'platform.discord.command.new.followup')

        tree.add_command(new_session, override=True)

        def restore():
            if tree.get_command('new') is new_session:
                tree.add_command(previous, override=True)

        def unload():
            # Plugin discovery/unload can run off-loop; SDK command tables belong to the bot loop.
            if not loop.is_closed():
                loop.call_soon_threadsafe(restore)

        ctx.on_unload(unload)

        async def resync():
            from gateway.run import _async_profile_runtime_scope
            # Startup may already be syncing the native schema; reconcile this schema afterwards.
            startup = adapter._post_connect_task
            if startup is not None and not startup.done():
                await asyncio.shield(startup)
            async with _async_profile_runtime_scope(owner):
                await adapter._run_post_connect_initialization()

        ctx.spawn_task(resync(), name='newprompt:discord-schema')

    # The host keys wired factories by qualname; a fresh load must rewire after its old cleanup.
    wire.__qualname__ += '.' + instance
    ctx.register_platform_handler('discord', wire)
