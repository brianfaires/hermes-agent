"""Read-only transcript reconstruction for the Hindsight history plugin."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

_MAX_CHARS = 500


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
    tool_calls: list[ToolCall] = field(default_factory=list)


def parse_options(raw_args: str) -> HistoryOptions:
    retain_only = recall_only = False
    turns: int | None = None
    words = raw_args.split()
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--retain-only":
            retain_only = True
        elif word == "--recall-only":
            recall_only = True
        elif word == "--turns":
            index += 1
            if index >= len(words):
                raise ValueError("`--turns` requires a positive whole number.")
            try:
                turns = int(words[index])
            except ValueError as exc:
                raise ValueError("`--turns` requires a positive whole number.") from exc
            if turns < 1:
                raise ValueError("`--turns` requires a positive whole number.")
        else:
            raise ValueError("Usage: `/hindsight-history [--retain-only|--recall-only] [--turns N]`")
        index += 1
    if retain_only and recall_only:
        raise ValueError("Choose either `--retain-only` or `--recall-only`, not both.")
    return HistoryOptions(retain_only, recall_only, turns)


def load_messages(options: HistoryOptions, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Load the latest persisted session, or the final N persisted user turns."""
    db_path = db_path or Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "state.db"
    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            if options.turns is None:
                session = conn.execute(
                    "SELECT s.id FROM sessions AS s "
                    "ORDER BY COALESCE((SELECT MAX(m.timestamp) FROM messages AS m "
                    "WHERE m.session_id = s.id), s.started_at) DESC, s.rowid DESC LIMIT 1"
                ).fetchone()
                if not session:
                    return []
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                    (session["id"],),
                ).fetchall()
            else:
                users = conn.execute(
                    "SELECT id FROM messages WHERE role = 'user' AND active = 1 "
                    "ORDER BY id DESC LIMIT ?", (options.turns,)
                ).fetchall()
                if not users:
                    return []
                first_id = min(row["id"] for row in users)
                rows = conn.execute(
                    "SELECT * FROM messages WHERE id >= ? AND active = 1 ORDER BY id",
                    (first_id,),
                ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    messages = []
    for row in rows:
        message = dict(row)
        if message.get("tool_calls"):
            try:
                message["tool_calls"] = json.loads(message["tool_calls"])
            except (TypeError, json.JSONDecodeError):
                message["tool_calls"] = []
        messages.append(message)
    return messages


def build_turns(messages: Sequence[dict[str, Any]]) -> list[HistoryTurn]:
    turns: list[HistoryTurn] = []
    current: HistoryTurn | None = None
    pending: dict[str, tuple[HistoryTurn, int]] = {}
    for message in messages:
        if message.get("role") == "user":
            current = HistoryTurn(str(message.get("content") or ""))
            turns.append(current)
            continue
        if current is None:
            continue
        if message.get("role") == "assistant":
            for call_id, call in _tool_calls(message):
                current.tool_calls.append(call)
                if call_id:
                    pending[call_id] = (current, len(current.tool_calls) - 1)
        elif message.get("role") == "tool":
            target = pending.get(str(message.get("tool_call_id") or ""))
            if target:
                turn, position = target
                call = turn.tool_calls[position]
                turn.tool_calls[position] = ToolCall(call.name, call.arguments, str(message.get("content") or ""))
    return turns


def render_history(
    turns: Sequence[HistoryTurn], options: HistoryOptions, *, auto_retain: bool,
    auto_recall: bool, recall: Callable[[str], Sequence[str]] | None,
) -> str:
    scope = "latest persisted session" if options.turns is None else f"last {options.turns} persisted turn(s)"
    lines = ["## Hindsight history", f"*Scope: {scope}. Explicit calls are recorded; automatic activity is reconstructed.*"]
    if not turns:
        return "\n".join(lines + ["", "No completed user turns found."])
    for number, turn in enumerate(turns, 1):
        lines.extend(["", f"### Turn {number}", f"**Message:** {_compact(turn.user_message)}"])
        if not options.recall_only:
            lines.append("**Retain**")
            lines.append("- automatic — reconstructed as enabled; asynchronous result was not recorded" if auto_retain else "- automatic — disabled in current configuration")
            _render_calls(lines, turn.tool_calls, "hindsight_retain")
        if not options.retain_only:
            lines.append("**Recall**")
            if auto_recall and recall:
                try:
                    memories = list(recall(turn.user_message))
                except Exception as exc:
                    lines.append(f"- automatic — reconstruction failed: {_compact(str(exc))}")
                else:
                    if memories:
                        lines.append("- automatic — re-run against the current memory store:")
                        lines.extend(f"  - {_compact(memory)}" for memory in memories)
                    else:
                        lines.append("- automatic — re-run returned no memories")
            else:
                lines.append("- automatic — disabled or unavailable")
            _render_calls(lines, turn.tool_calls, "hindsight_recall")
    return "\n".join(lines)


def _render_calls(lines: list[str], calls: Iterable[ToolCall], name: str) -> None:
    matching = [call for call in calls if call.name == name]
    if not matching:
        lines.append("- explicit — none")
        return
    for call in matching:
        value = call.arguments.get("query") or call.arguments.get("content") or ""
        lines.append(f"- explicit: `{name}` — {_compact(str(value))}")
        lines.append(f"  - returned: {_compact(call.result) if call.result else 'no persisted tool result'}")


def _tool_calls(message: dict[str, Any]) -> Iterable[tuple[str, ToolCall]]:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        return []
    result = []
    for raw in calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = str(function.get("name") or raw.get("name") or "") if isinstance(function, dict) else ""
        if not name:
            continue
        arguments = function.get("arguments") or raw.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        result.append((str(raw.get("call_id") or raw.get("id") or ""), ToolCall(name, arguments if isinstance(arguments, dict) else {})))
    return result


def _compact(value: str) -> str:
    value = " ".join(value.split())
    return value if len(value) <= _MAX_CHARS else f"{value[:_MAX_CHARS - 1]}…"
