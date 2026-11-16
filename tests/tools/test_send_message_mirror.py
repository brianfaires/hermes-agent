"""Mirror lookup for sends from a gateway session (hermes-shared#88)."""

from unittest.mock import patch

import pytest

from tools.send_message_tool import _mirror_sent_message


@pytest.mark.parametrize("target, expected_user", [
    ("telegram", None),
    ("Discord", "1498099210048766032"),
])
def test_user_filter_only_on_session_platform(monkeypatch, target, expected_user):
    """A Discord session whose turn another bot started forwards to Telegram: its
    Discord user id must not filter the Telegram lookup, or the copy finds no session."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "1498099210048766032")
    with patch("gateway.mirror.mirror_to_session", return_value=True) as mirror:
        assert _mirror_sent_message(target, "8244556262", "hi", None)

    assert mirror.call_args.kwargs["user_id"] == expected_user
    assert mirror.call_args.kwargs["source_label"] == "discord"
