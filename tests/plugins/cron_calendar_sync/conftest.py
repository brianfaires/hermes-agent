"""Make the cron-calendar-sync plugin importable as a package for its tests.

The plugin dir name has hyphens (not a valid module name), and calendar_sync
uses a relative ``from . import calendar_client``. Mirror how the hosting
PluginManager loads the plugin: register it under
``hermes_plugins.cron_calendar_sync`` with the plugin dir as its package path,
so tests can ``from hermes_plugins.cron_calendar_sync import calendar_sync``.
"""

import importlib.util
import sys
import types
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "cron-calendar-sync"
_PKG = "hermes_plugins.cron_calendar_sync"


def _ensure_loaded():
    if _PKG in sys.modules:
        return
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        _PKG, _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _PKG
    mod.__path__ = [str(_PLUGIN_DIR)]
    sys.modules[_PKG] = mod
    spec.loader.exec_module(mod)


_ensure_loaded()
