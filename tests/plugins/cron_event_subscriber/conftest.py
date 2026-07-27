"""Load the hyphenated bundled plugin as a package for focused tests."""

import importlib.util
import sys
import types
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "cron-event-subscriber"
_PKG = "hermes_plugins.cron_event_subscriber"


def _ensure_loaded():
    root = sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    if not hasattr(root, "__path__"):
        root.__path__ = []
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PKG,
            _PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(_PLUGIN_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        spec.loader.exec_module(module)


_ensure_loaded()
