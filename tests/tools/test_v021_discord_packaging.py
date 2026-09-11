"""Installer surfaces agree on explicit pip-compatible Discord requirements."""
from pathlib import Path
import shlex
import tomllib
import unittest
from packaging.requirements import Requirement
from tools.lazy_deps import LAZY_DEPS, feature_install_command

class DiscordPackagingTests(unittest.TestCase):
    def test_lazy_and_manifest_specs_agree_for_pip_and_uv_commands(self):
        project = tomllib.loads((Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text())
        manifest = {r.name.lower(): r for r in map(Requirement, project['project']['optional-dependencies']['messaging'])}
        specs = LAZY_DEPS['platform.discord']
        for text in specs:
            req = Requirement(text)
            self.assertEqual(str(req.specifier), str(manifest[req.name.lower()].specifier))
        self.assertFalse(manifest['discord.py'].extras)
        self.assertIn('pynacl', manifest); self.assertIn('davey', manifest)
        for pip in (True, False):
            command = shlex.split(feature_install_command('platform.discord', venv_pip=pip))
            self.assertEqual(command[command.index('install') + 1:], list(specs))
