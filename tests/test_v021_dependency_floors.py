"""Base-install requirements retain the historical qualified ASGI floors."""
from pathlib import Path
import tomllib
import unittest
from packaging.requirements import Requirement


class DependencyFloors(unittest.TestCase):
    def test_base_install_excludes_pre_restoration_asgi_versions(self):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
        requirements = {req.name: req for req in map(Requirement, project['dependencies'])}
        # These are historical safety floors, not a snapshot of latest versions.
        for name, rejected in [('starlette', '1.3.0'), ('python-multipart', '0.0.30')]:
            with self.subTest(package=name):
                self.assertIn(name, requirements)
                self.assertNotIn(rejected, requirements[name].specifier)
        locked = tomllib.loads((root / 'uv.lock').read_text())['package']
        for name in ('starlette', 'python-multipart'):
            selected = next(package for package in locked if package['name'] == name)
            self.assertIn(selected['version'], requirements[name].specifier)


if __name__ == '__main__': unittest.main()
