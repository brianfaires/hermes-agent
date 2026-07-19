"""Read-only reconstruction and rendering for Hindsight slash-command history.

Automatic Hindsight retrieval results were not persisted historically.  This
module intentionally labels newly-issued searches as reconstructed so callers
never mistake current-store results for an event audit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


_MAX_DISPLAY_CHARS = 500


@dataclass(frozen=True)
class HistoryOptions:
    retain_only: bool = False
    recall_only: bool = False
    turns: int | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: str = ""


@dataclass
class HistoryTurn:
    user_message: str
    timestamp: float | None
    assistant_message: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def parse_history_options(raw_args: str) -> HistoryOptions:
    """Parse the deliberately small ``/hindsight-history`` argument grammar."""
    retain_only = False
    recall_only = False
    turns: int | None = None
    tokens = raw_args.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--retain-only":
            retain_only = True
        elif token == "--recall-only":
            recall_only = True
        elif token == "--turns":
            index += 1
            if index >= len(tokens):
                raise ValueError("`--turns` requires a positive whole number.")
            try:
                turns = int(tokens[index])
            except ValueError as exc:
                raise ValueError("`--turns` requires a positive whole number.") from exc
            if turns < 1:
                raise ValueError("`--turns` requires a positive whole number.")
        else:
            raise ValueError(
                "Usage: `/hindsight-history [--retain-only|--recall-only] [--turns N]`"
            )
        index += 1
    if retain_only and recall_only:
        raise ValueError("Choose either `--retain-only` or `--recall-only`, not both.")
    return HistoryOptions(retain_only=retain_only, recall_only=recall_only, turns=turns)


def build_turns(messages: Sequence[dict[str, Any]]) -> list[HistoryTurn]:
    """Group persisted message rows into user-initiated turns.

    Tool calls are stored on assistant rows and their response rows follow later
    in the transcript.  This preserves exact explicit Hindsight tool activity;
    automatic activity is reconstructed separately by the caller.
    """
    turns: list[HistoryTurn] = []
    current: HistoryTurn | None = None
    pending: dict[str, tuple[HistoryTurn, int]] = {}

    for message in messages:
        role = message.get("role")
        if role == "user":
            current = HistoryTurn(
                user_message=str(message.get("content") or ""),
                timestamp=_as_timestamp(message.get("timestamp")),
            )
            turns.append(current)
            continue
        if current is None:
            continue
        if role == "assistant":
            content = str(message.get("content") or "")
            if content:
                current.assistant_message = _join_text(current.assistant_message, content)
            for call_id, call in _tool_calls(message):
                current.tool_calls.append(call)
                if call_id:
                    pending[call_id] = (current, len(current.tool_calls) - 1)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            target = pending.get(call_id)
            if target:
                turn, position = target
                prior = turn.tool_calls[position]
                turn.tool_calls[position] = ToolCall(
                    name=prior.name,
                    arguments=prior.arguments,
                    result=str(message.get("content") or ""),
                )

    return turns


def render_history(
    turns: Sequence[HistoryTurn],
    options: HistoryOptions,
    *,
    auto_retain: bool,
    retain_every_n_turns: int,
    auto_recall: bool,
    recall: Callable[[str], Sequence[str]] | None = None,
) -> str:
    """Render a readable report, keeping factual and reconstructed data distinct."""
    lines = ["## Hindsight history", "*Explicit tool calls are recorded. Automatic activity is reconstructed from the current configuration/store.*"]
    if not turns:
        return "\n".join(lines + ["", "No completed user turns found."])

    for number, turn in enumerate(turns, 1):
        lines.extend(["", f"### Turn {number}", f"**Message:** {_compact(turn.user_message)}"])
        if not options.recall_only:
            lines.append("**Retain**")
            if auto_retain:
                cadence = max(1, retain_every_n_turns)
                if cadence == 1:
                    lines.append("- automatic — reconstructed as queued; asynchronous result was not recorded")
                else:
                    lines.append(
                        f"- automatic — eligible under the current every-{cadence}-turn cadence; "
                        "the exact enqueue event was not recorded"
                    )
            else:
                lines.append("- automatic — disabled in current configuration")
            _render_calls(lines, turn.tool_calls, "hindsight_retain", label="explicit")
        if not options.retain_only:
            lines.append("**Recall**")
            if auto_recall and recall is not None:
                try:
                    memories = list(recall(turn.user_message))
                except Exception as exc:
                    lines.append(f"- automatic — reconstruction failed: {_compact(str(exc))}")
                else:
                    if memories:
                        lines.append("- automatic — reconstructed against the current memory store:")
                        lines.extend(f"  - {_compact(memory)}" for memory in memories)
                    else:
                        lines.append("- automatic — reconstructed search returned no memories")
            elif auto_recall:
                lines.append("- automatic — unavailable; no active Hindsight provider")
            else:
                lines.append("- automatic — disabled in current configuration")
            _render_calls(lines, turn.tool_calls, "hindsight_recall", label="explicit")
    return "\n".join(lines)


def _render_calls(lines: list[str], calls: Iterable[ToolCall], name: str, *, label: str) -> None:
    matching = [call for call in calls if call.name == name]
    if not matching:
        lines.append(f"- {label} — none")
        return
    for call in matching:
        query_or_content = call.arguments.get("query") or call.arguments.get("content") or ""
        lines.append(f"- {label}: `{name}` — {_compact(str(query_or_content))}")
        if call.result:
            lines.append(f"  - returned: {_compact(call.result)}")
        else:
            lines.append("  - returned: no persisted tool result")


def _tool_calls(message: dict[str, Any]) -> Iterable[tuple[str, ToolCall]]:
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        return []
    parsed: list[tuple[str, ToolCall]] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or raw.get("name") or "")
        if not name:
            continue
        arguments = function.get("arguments") or raw.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        if not isinstance(arguments, dict):
            arguments = {"raw": str(arguments)}
        call_id = str(raw.get("call_id") or raw.get("id") or "")
        parsed.append((call_id, ToolCall(name=name, arguments=arguments)))
    return parsed


def _as_timestamp(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _join_text(first: str, second: str) -> str:
    return f"{first}\n{second}".strip() if first else second


def _compact(value: str) -> str:
    value = " ".join(value.split())
    return value if len(value) <= _MAX_DISPLAY_CHARS else f"{value[:_MAX_DISPLAY_CHARS - 1]}…"
