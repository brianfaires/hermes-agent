"""Session-static schema visibility preferences; toolsets still own authorization."""
import logging

logger = logging.getLogger("tools.tool_search")


def _platform_pins(platform: str) -> frozenset[str]:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    tools = config.get("tools")
    search = tools.get("tool_search") if isinstance(tools, dict) else None
    pins = search.get("pinned", {}) if isinstance(search, dict) else {}
    if not isinstance(pins, dict):
        logger.warning("tools.tool_search.pinned must map platform names to lists of tool names")
        return frozenset()
    names = pins.get(platform, [])
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        logger.warning("tools.tool_search.pinned.%s must be a list of tool names", platform)
        return frozenset()
    return frozenset(name.strip() for name in names if name.strip())


def agent_pinned_tools(agent) -> frozenset[str]:
    """Resolve once for this agent; a config edit takes effect in a new conversation."""
    pins = getattr(agent, "_tool_search_pinned_tools", None)
    if not isinstance(pins, frozenset):
        platform = str(getattr(agent, "platform", None) or "cli").strip().lower()
        pins = _platform_pins(platform)
        agent._tool_search_pinned_tools = pins
    return pins


def pin_preferences(agent) -> dict:
    """Keep the construction-time choice alongside the native full-schema session pin."""
    pins = getattr(agent, "_tool_search_pinned_tools", None)
    return {"schema_pins": sorted(pins)} if isinstance(pins, frozenset) else {}


def restore_pin_preferences(agent, saved) -> None:
    """Rebuild only a changed visibility preference before native byte-prefix restoration."""
    pins = saved.get("schema_pins") if isinstance(saved, dict) else None
    if not isinstance(pins, list) or any(not isinstance(name, str) for name in pins):
        return
    pins = frozenset(pins)
    if pins != agent_pinned_tools(agent):
        agent._tool_search_pinned_tools = pins
        from tools.mcp_tool_agent import refresh_agent_mcp_tools
        refresh_agent_mcp_tools(agent, content_aware=True)
