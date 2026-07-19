import pytest

from hermes_state import SessionDB
from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight.history import (
    build_turns,
    parse_history_options,
    render_history,
)


def _messages():
    return [
        {"role": "user", "content": "remember the launch plan", "timestamp": 1},
        {
            "role": "assistant",
            "content": "I will retain it.",
            "tool_calls": [
                {
                    "call_id": "retain-1",
                    "function": {
                        "name": "hindsight_retain",
                        "arguments": '{"content":"launch plan"}',
                    },
                },
                {
                    "call_id": "recall-1",
                    "function": {
                        "name": "hindsight_recall",
                        "arguments": '{"query":"launch"}',
                    },
                },
            ],
        },
        {"role": "tool", "tool_call_id": "retain-1", "content": '{"result":"stored"}'},
        {"role": "tool", "tool_call_id": "recall-1", "content": '{"result":"one. memory"}'},
        {"role": "user", "content": "what is next?", "timestamp": 2},
        {"role": "assistant", "content": "Deploy."},
    ]


def test_build_turns_preserves_explicit_hindsight_calls_and_results():
    turns = build_turns(_messages())

    assert len(turns) == 2
    assert turns[0].assistant_message == "I will retain it."
    assert [(call.name, call.result) for call in turns[0].tool_calls] == [
        ("hindsight_retain", '{"result":"stored"}'),
        ("hindsight_recall", '{"result":"one. memory"}'),
    ]


def test_render_history_distinguishes_recorded_and_reconstructed_activity():
    report = render_history(
        build_turns(_messages()),
        parse_history_options(""),
        auto_retain=True,
        retain_every_n_turns=1,
        auto_recall=True,
        recall=lambda query: [f"current match for {query}"],
    )

    assert "automatic — reconstructed as queued" in report
    assert "explicit: `hindsight_retain` — launch plan" in report
    assert "returned: {\"result\":\"stored\"}" in report
    assert "automatic — reconstructed against the current memory store" in report
    assert "current match for remember the launch plan" in report
    assert "explicit: `hindsight_recall` — launch" in report


def test_render_history_filter_flags_hide_the_other_section():
    turns = build_turns(_messages())
    retain = render_history(
        turns, parse_history_options("--retain-only"), auto_retain=True,
        retain_every_n_turns=1, auto_recall=True, recall=lambda _: ["memory"],
    )
    recall = render_history(
        turns, parse_history_options("--recall-only"), auto_retain=True,
        retain_every_n_turns=1, auto_recall=True, recall=lambda _: ["memory"],
    )

    assert "**Retain**" in retain and "**Recall**" not in retain
    assert "**Recall**" in recall and "**Retain**" not in recall


@pytest.mark.parametrize("raw", ["--turns", "--turns 0", "--turns nope", "--retain-only --recall-only"])
def test_parse_history_options_rejects_invalid_arguments(raw):
    with pytest.raises(ValueError):
        parse_history_options(raw)


def test_parse_history_options_accepts_turn_span():
    assert parse_history_options("--turns 4").turns == 4


def test_reconstruct_recall_replays_reflect_mode():
    provider = HindsightMemoryProvider()
    provider._memory_mode = "hybrid"
    provider._auto_recall = True
    provider._prefetch_method = "reflect"
    provider._bank_id = "bank"
    provider._budget = "mid"

    class Client:
        def areflect(self, **kwargs):
            assert kwargs == {"bank_id": "bank", "query": "what changed", "budget": "mid"}
            return type("Response", (), {"text": "synthesized memory"})()

    provider._run_hindsight_operation = lambda operation: operation(Client())

    assert provider.reconstruct_recall("what changed") == ["synthesized memory"]


def test_session_key_history_uses_insertion_order_not_unreliable_timestamps(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("older", source="discord", session_key="same-chat")
        db.append_message("older", "user", "first", timestamp=200)
        db.create_session("newer", source="discord", session_key="same-chat")
        db.append_message("newer", "user", "second", timestamp=100)
        # `/resume` can append to an older session after another one ran.
        db.append_message("older", "user", "resumed", timestamp=50)

        assert [row["content"] for row in db.get_messages_for_session_key("same-chat")] == [
            "first", "second", "resumed"
        ]
    finally:
        db.close()
