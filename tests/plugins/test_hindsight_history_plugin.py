import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).parents[2] / "plugins" / "hindsight-history"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


history = _load_module("test_hindsight_history.history", PLUGIN_DIR / "history.py")
plugin = _load_module("test_hindsight_history", PLUGIN_DIR / "__init__.py")


def _messages():
    return [
        {"role": "user", "content": "remember the launch plan"},
        {
            "role": "assistant",
            "tool_calls": [
                {"call_id": "retain-1", "function": {"name": "hindsight_retain", "arguments": '{"content":"launch plan"}'}},
                {"call_id": "recall-1", "function": {"name": "hindsight_recall", "arguments": '{"query":"launch"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "retain-1", "content": '{"result":"stored"}'},
        {"role": "tool", "tool_call_id": "recall-1", "content": '{"result":"one memory"}'},
        {"role": "user", "content": "what is next?"},
    ]


def test_build_turns_preserves_explicit_hindsight_calls_and_results():
    turns = history.build_turns(_messages())
    assert len(turns) == 2
    assert [(call.name, call.result) for call in turns[0].tool_calls] == [
        ("hindsight_retain", '{"result":"stored"}'),
        ("hindsight_recall", '{"result":"one memory"}'),
    ]


def test_render_history_distinguishes_recorded_and_reconstructed_activity():
    report = history.render_history(
        history.build_turns(_messages()), history.parse_options(""), auto_retain=True,
        auto_recall=True, recall=lambda query: [f"current match for {query}"],
    )
    assert "latest persisted session" in report
    assert "automatic — reconstructed as enabled" in report
    assert "explicit: `hindsight_retain` — launch plan" in report
    assert "re-run against the current memory store" in report


def test_filter_flags_hide_the_other_section():
    turns = history.build_turns(_messages())
    retain = history.render_history(turns, history.parse_options("--retain-only"), auto_retain=True, auto_recall=True, recall=None)
    recall = history.render_history(turns, history.parse_options("--recall-only"), auto_retain=True, auto_recall=True, recall=None)
    assert "**Retain**" in retain and "**Recall**" not in retain
    assert "**Recall**" in recall and "**Retain**" not in recall


@pytest.mark.parametrize("raw", ["--turns", "--turns 0", "--turns nope", "--retain-only --recall-only"])
def test_parse_options_rejects_invalid_arguments(raw):
    with pytest.raises(ValueError):
        history.parse_options(raw)


def test_load_messages_defaults_to_latest_session_and_turns_span(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,
                tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL
            );
            """
        )
        conn.executemany("INSERT INTO sessions VALUES (?, ?)", [("old", 1), ("new", 2)])
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, 1)",
            [
                ("new", "user", "new turn one", 2),
                ("old", "user", "old interleaved turn", 3),
                ("new", "assistant", "new response", 4),
                ("new", "user", "new turn two", 5),
            ],
        )

    assert [row["content"] for row in history.load_messages(history.parse_options(""), db_path)] == [
        "new turn one", "new response", "new turn two"
    ]
    assert [row["content"] for row in history.load_messages(history.parse_options("--turns 2"), db_path)] == [
        "new turn one", "new response", "new turn two"
    ]


def test_registers_plugin_command():
    class Context:
        def __init__(self):
            self.commands = []

        def register_command(self, *args, **kwargs):
            self.commands.append((args, kwargs))

    context = Context()
    plugin.register(context)
    assert context.commands[0][0][0] == "hindsight-history"
