"""Native runtime mirrors shared by scalar and structural config edits."""
from __future__ import annotations

from hermes_constants import set_hermes_home_override, reset_hermes_home_override

_MISSING = object()


def sync_config_value(key, value=_MISSING):
    from hermes_cli import config
    env_var = config.terminal_config_env_var_for_key(key)
    if env_var and key != "terminal.cwd":
        if value is _MISSING:
            config.remove_env_value(env_var)
        else:
            config.save_env_value(env_var, config._terminal_env_value(value))
    if value is not _MISSING:
        config._touch_skin_file(key, value)
    if key == "display.language":
        from agent.i18n import reset_language_cache
        reset_language_cache()


def sync_config_changes(path, before, after):
    """Compare final state once: parent replacement/removal owns its leaf effects too."""
    from hermes_cli import config
    token = set_hermes_home_override(path.parent)
    try:
        keys = ["terminal." + key for key in config.TERMINAL_CONFIG_ENV_MAP]
        keys.extend(("display.skin", "display.language"))
        for key in keys:
            old, new = (config._get_nested(state, key) for state in (before, after))
            if old == new:
                continue
            sync_config_value(key, _MISSING if new is config._MISSING else new)
    finally:
        reset_hermes_home_override(token)
