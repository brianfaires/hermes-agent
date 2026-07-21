"""Import-safety tests for the Discord gateway adapter."""

import subprocess
import sys
import textwrap


class TestDiscordImportSafety:
    def test_module_imports_even_when_discord_dependency_is_missing(self):
        script = textwrap.dedent(
            """
            import builtins
            import importlib

            original_import = builtins.__import__

            def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "discord" or name.startswith("discord."):
                    raise ImportError("discord unavailable for test")
                return original_import(name, globals, locals, fromlist, level)

            builtins.__import__ = fake_import
            module = importlib.import_module("plugins.platforms.discord.adapter")
            assert module.DISCORD_AVAILABLE is False
            assert module.discord is None
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
