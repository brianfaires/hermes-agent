"""Optional slash-command plugin for browsing reconstructed Hindsight activity."""

from __future__ import annotations

import json
from typing import Any

from plugins.memory.hindsight import HindsightMemoryProvider

from .history import build_turns, load_messages, parse_options, render_history


def _handle_history(raw_args: str) -> str:
    try:
        options = parse_options(raw_args)
    except ValueError as exc:
        return str(exc)

    provider = HindsightMemoryProvider()
    try:
        # Use the provider's existing `hindsight_recall` implementation rather
        # than reconstructing Hindsight client requests in this viewer.
        provider.initialize("hindsight-history")
    except Exception as exc:
        return f"Hindsight history is unavailable: {exc}"

    def recall(query: str) -> list[str]:
        response = provider.handle_tool_call("hindsight_recall", {"query": query})
        try:
            text = str(json.loads(response).get("result") or "")
        except (TypeError, json.JSONDecodeError):
            text = str(response or "")
        if not text or text == "No relevant memories found.":
            return []
        return [line.lstrip("- ").strip() for line in text.splitlines() if line.strip()]

    return render_history(
        build_turns(load_messages(options)),
        options,
        auto_retain=bool(provider._auto_retain),
        auto_recall=bool(provider._auto_recall) and provider._memory_mode != "tools",
        recall=recall,
    )


def register(ctx: Any) -> None:
    ctx.register_command(
        "hindsight-history",
        handler=_handle_history,
        description="Show reconstructed Hindsight retain and recall activity",
    )
