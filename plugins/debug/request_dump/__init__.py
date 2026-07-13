"""Dump the newest persisted system prompt with currently assembled tools."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_LOCK = threading.Lock()
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_METADATA_COLUMNS = (
    "id", "model", "source", "started_at", "title", "model_config", "cwd",
    "parent_session_id", "billing_provider", "billing_mode",
)
_WRAP_WIDTH = 100


def _hermes_home() -> Path:
    return get_hermes_home().resolve()


def _profile() -> str:
    return os.environ.get("HERMES_PROFILE", "default") or "default"


def _latest_session(db_path: Path) -> dict[str, Any] | None:
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if not {"system_prompt", "started_at"}.issubset(columns):
                return None
            selected = [name for name in _METADATA_COLUMNS if name in columns]
            if "id" not in selected:
                return None
            names = selected + ["system_prompt"]
            quoted = ", ".join(f'"{name}"' for name in names)
            row = conn.execute(
                f"SELECT {quoted} FROM sessions "
                "WHERE system_prompt IS NOT NULL AND length(system_prompt) > 0 "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return dict(zip(names, row)) if row else None


def _toolsets(source: str) -> tuple[list[str], list[str]]:
    """Resolve configuration exactly as gateway AIAgent construction does."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools

    config = load_config() or {}
    enabled = sorted(_get_platform_tools(config, source or "cli"))
    agent = config.get("agent") or {}
    disabled = list(agent.get("disabled_toolsets") or [])
    return enabled, disabled


def _tools(enabled: list[str], disabled: list[str]) -> list[dict[str, Any]]:
    from model_tools import get_tool_definitions

    return get_tool_definitions(
        enabled_toolsets=enabled,
        disabled_toolsets=disabled or None,
        quiet_mode=True,
        skip_tool_search_assembly=False,
    )


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _estimate_tokens(chars: int) -> int:
    return max(1, chars // 4)


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "0.0%"
    return f"{(part / whole) * 100:.1f}%"


def _readable_string(value: str) -> str:
    readable = value.replace("\\r\\n", "\n")
    readable = readable.replace("\\n", "\n").replace("\\r", "\n")
    return readable.replace('\\"', '"')


def _wrap_line(line: str, width: int = _WRAP_WIDTH) -> str:
    if len(line) <= width:
        return line
    chunks: list[str] = []
    remaining = line
    while len(remaining) > width:
        split_at = remaining.find(" ", width)
        if split_at == -1:
            chunks.append(remaining)
            return "\n".join(chunks)
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at + 1 :]
    chunks.append(remaining)
    return "\n".join(chunks)


def _wrap_text(text: str, width: int = _WRAP_WIDTH) -> str:
    return "\n".join(_wrap_line(line, width) for line in text.splitlines())


def _dump_root() -> Path:
    return (_hermes_home() / "dump-system-prompt").resolve()


def _readable_json(value: Any, indent: int = 0) -> str:
    """Render schema-shaped data readably rather than as JSON-escaped text."""
    pad = " " * indent
    if isinstance(value, dict):
        lines = ["{"]
        items = list(value.items())
        for index, (key, child) in enumerate(items):
            suffix = "," if index < len(items) - 1 else ""
            lines.append(f'{" " * (indent + 2)}"{key}": {_readable_json(child, indent + 2)}{suffix}')
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = ["["]
        for index, child in enumerate(value):
            suffix = "," if index < len(value) - 1 else ""
            lines.append(f'{" " * (indent + 2)}{_readable_json(child, indent + 2)}{suffix}')
        lines.append(f"{pad}]")
        return "\n".join(lines)
    if isinstance(value, str):
        return f'"{_readable_string(value)}"'
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _started_at(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).astimezone().isoformat()
    except (TypeError, ValueError, OSError):
        return str(value or "(unknown)")


def _skills_chars(prompt: str) -> int:
    start = prompt.find("<available_skills>")
    if start < 0:
        return 0
    start += len("<available_skills>")
    end = prompt.find("</available_skills>", start)
    return len(prompt[start:end]) if end >= 0 else 0


def _header(row: dict[str, Any], enabled: list[str], disabled: list[str], tools: list[dict[str, Any]]) -> str:
    prompt = row["system_prompt"]
    prompt_chars = len(prompt)
    skills_chars = _skills_chars(prompt)
    non_skills_prompt_chars = prompt_chars - skills_chars
    tool_rows = []
    for index, tool in enumerate(tools):
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = function.get("name") if isinstance(function, dict) else None
        size = _json_size(tool)
        tool_rows.append((str(name or f"tool-{index + 1}"), size))
    tool_rows.sort(key=lambda item: item[1], reverse=True)
    tools_chars = sum(size for _, size in tool_rows)
    total_chars = non_skills_prompt_chars + skills_chars + tools_chars
    lines = [
        "Hermes system prompt estimate",
        "basis: persisted session prompt + current tool configuration (not an original request capture)",
        f"profile: {_profile()}",
        f"HERMES_HOME: {_hermes_home()}",
        f"session_id: {row.get('id') or '(unknown)'}",
        f"title: {row.get('title') or '(none)'}",
        f"model: {row.get('model') or '(unknown)'}",
        f"source/platform: {row.get('source') or '(unknown)'}",
        f"started_at: {_started_at(row.get('started_at'))}",
        f"enabled_toolsets: {', '.join(enabled) or '(none)'}",
        f"disabled_toolsets: {', '.join(disabled) or '(none)'}",
        f"tool_count: {len(tool_rows)}",
        (
            f"prompt_footprint: {non_skills_prompt_chars} chars / "
            f"~{_estimate_tokens(non_skills_prompt_chars)} tokens "
            f"({_pct(non_skills_prompt_chars, total_chars)} of total)"
        ),
        (
            f"tools_footprint: {tools_chars} chars / ~{_estimate_tokens(tools_chars)} tokens "
            f"({_pct(tools_chars, total_chars)} of total)"
        ),
        (
            f"skills_footprint: {skills_chars} chars / ~{_estimate_tokens(skills_chars)} tokens "
            f"({_pct(skills_chars, total_chars)} of total)"
        ),
        f"estimated_total_footprint: {total_chars} chars / ~{_estimate_tokens(total_chars)} tokens",
    ]
    for key in _METADATA_COLUMNS:
        if key not in {"id", "model", "source", "started_at", "title"} and row.get(key) not in (None, ""):
            lines.append(f"session_{key}: {row[key]}")
    lines.append("tools_by_serialized_size:")
    lines.extend(
        (
            f"  {name}: {size} chars / ~{_estimate_tokens(size)} tokens "
            f"({_pct(size, total_chars)} of total)"
        )
        for name, size in tool_rows
    )
    if not tool_rows:
        lines.append("  (none)")
    return "\n".join(lines)


def _write_exclusive(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def dump_latest(_raw_args: str = "") -> str:
    home = _hermes_home()
    row = _latest_session(home / "state.db")
    if row is None:
        return f"No persisted session with a non-empty system prompt was found in `{home / 'state.db'}`."

    source = str(row.get("source") or "cli")
    enabled, disabled = _toolsets(source)
    tools = _tools(enabled, disabled)
    prompt = _wrap_text(_readable_string(row["system_prompt"]))
    full_parts = [
        _header(row, enabled, disabled, tools),
        "System prompt:",
        prompt,
        "Current assembled tool definitions:",
        _wrap_text(_readable_json(tools)),
    ]
    full_text = "\n\n".join(full_parts) + "\n"

    output_dir = _dump_root()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    session = _SAFE_NAME.sub("_", str(row.get("id") or "unknown")).strip("_")
    session = (session or "unknown")[-12:]
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    with _LOCK:
        counter = 1
        while True:
            stem = f"{_profile()}-{timestamp}-{session}-{counter}"
            full_path = output_dir / f"{stem}.txt"
            if full_path.exists():
                counter += 1
                continue
            _write_exclusive(full_path, full_text)
            break

    return f"System prompt written: `{full_path}`"


def register(ctx: Any) -> None:
    ctx.register_command(
        "dump-system-prompt",
        dump_latest,
        description="Dump newest persisted system prompt with current assembled tools",
    )
