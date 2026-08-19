"""Tests for gateway/mirror.py — session mirroring."""

import json
from unittest.mock import patch, MagicMock

import gateway.mirror as mirror_mod
import hermes_state
from gateway.mirror import (
    mirror_to_session,
    _find_session_id,
)


def _setup_sessions(tmp_path, sessions_data):
    """Helper to write a fake sessions.json and patch module-level paths."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = sessions_dir / "sessions.json"
    index_file.write_text(json.dumps(sessions_data))
    return sessions_dir, index_file


def _create_gateway_session(
    db,
    session_id,
    *,
    profile_name,
    chat_id="8244556262",
    user_id="8244556262",
):
    namespace = "main" if profile_name == "default" else profile_name
    db.create_session(
        session_id=session_id,
        source="telegram",
        user_id=user_id,
        session_key=f"agent:{namespace}:telegram:dm:{user_id}",
        chat_id=chat_id,
        chat_type="dm",
        profile_name=profile_name,
    )
    db.append_message(session_id=session_id, role="user", content=f"{profile_name} fixture")


def _message_rows(db, session_id):
    return [dict(row) for row in db.get_messages(session_id, include_inactive=True)]


class TestFindSessionId:
    def test_finds_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "agent:main:telegram:dm": {
                "session_id": "sess_abc",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            }
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_abc"

    def test_returns_most_recent(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "old": {
                "session_id": "sess_old",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "new": {
                "session_id": "sess_new",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_new"

    def test_thread_id_disambiguates_same_chat(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "topic_a": {
                "session_id": "sess_topic_a",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "10"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "topic_b": {
                "session_id": "sess_topic_b",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "11"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "-1001", thread_id="10")

        assert result == "sess_topic_a"

    def test_user_id_disambiguates_same_group_chat(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "alice": {
                "session_id": "sess_alice",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "alice"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "bob": {
                "session_id": "sess_bob",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "bob"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "-1001", user_id="alice")

        assert result == "sess_alice"

    def test_ambiguous_same_group_chat_without_user_id_returns_none(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "alice": {
                "session_id": "sess_alice",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "alice"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "bob": {
                "session_id": "sess_bob",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "bob"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "-1001")

        assert result is None

    def test_no_match_returns_none(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "sess": {
                "session_id": "sess_1",
                "origin": {"platform": "discord", "chat_id": "999"},
                "updated_at": "2026-01-01T00:00:00",
            }
        })

        with patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result is None

    def test_missing_sessions_file(self, tmp_path):
        with patch.object(mirror_mod, "_SESSIONS_INDEX", tmp_path / "nope.json"):
            result = _find_session_id("telegram", "12345")

        assert result is None

    def test_platform_case_insensitive(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "s1": {
                "session_id": "sess_1",
                "origin": {"platform": "Telegram", "chat_id": "123"},
                "updated_at": "2026-01-01T00:00:00",
            }
        })

        with patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "123")

        assert result == "sess_1"



class TestMirrorToSession:
    def test_successful_mirror(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "s1": {
                "session_id": "sess_abc",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            }
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("gateway.mirror._append_to_sqlite") as mock_sqlite:
            result = mirror_to_session("telegram", "12345", "Hello!", source_label="cli")

        assert result is True

        # Check SQLite writer was called with the mirror message
        mock_sqlite.assert_called_once()
        call_args = mock_sqlite.call_args
        assert call_args[0][0] == "sess_abc"
        msg = call_args[0][1]
        assert msg["content"] == "Hello!"
        assert msg["role"] == "assistant"
        assert msg["mirror"] is True
        assert msg["mirror_source"] == "cli"

    def test_successful_mirror_uses_thread_id(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "topic_a": {
                "session_id": "sess_topic_a",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "10"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "topic_b": {
                "session_id": "sess_topic_b",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "11"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("gateway.mirror._append_to_sqlite") as mock_sqlite:
            result = mirror_to_session("telegram", "-1001", "Hello topic!", source_label="cron", thread_id="10")

        assert result is True
        mock_sqlite.assert_called_once()
        assert mock_sqlite.call_args[0][0] == "sess_topic_a"

    def test_successful_mirror_uses_user_id_for_group_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "alice": {
                "session_id": "sess_alice",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "alice"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "bob": {
                "session_id": "sess_bob",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "bob"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("gateway.mirror._append_to_sqlite") as mock_sqlite:
            result = mirror_to_session(
                "telegram",
                "-1001",
                "Hello group!",
                source_label="cli",
                user_id="alice",
            )

        assert result is True
        mock_sqlite.assert_called_once()
        assert mock_sqlite.call_args[0][0] == "sess_alice"

    def test_no_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {})

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = mirror_to_session("telegram", "99999", "Hello!")

        assert result is False

    def test_error_returns_false(self, tmp_path):
        with patch("gateway.mirror._find_session_id", side_effect=Exception("boom")):
            result = mirror_to_session("telegram", "123", "msg")

        assert result is False

    def test_profile_scoped_mirror_does_not_contaminate_newer_same_peer_sessions(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
        db = hermes_state.SessionDB()
        try:
            _create_gateway_session(db, "sess_default", profile_name="default")
            _create_gateway_session(db, "sess_ang", profile_name="ang")
            _create_gateway_session(db, "sess_ops", profile_name="ops")
            before_default = _message_rows(db, "sess_default")
            before_ang = _message_rows(db, "sess_ang")
            before_ops = _message_rows(db, "sess_ops")

            result = mirror_to_session(
                "telegram",
                "8244556262",
                "default standalone reminder",
                source_label="cron",
                user_id="8244556262",
                role="user",
                profile_name="default",
            )

            assert result is True
            after_default = _message_rows(db, "sess_default")
            assert after_default[:-1] == before_default
            assert after_default[-1]["role"] == "user"
            assert after_default[-1]["content"] == "default standalone reminder"
            assert _message_rows(db, "sess_ang") == before_ang
            assert _message_rows(db, "sess_ops") == before_ops
        finally:
            db.close()

    def test_profile_scoped_mirror_skips_when_same_profile_session_is_absent(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
        db = hermes_state.SessionDB()
        try:
            _create_gateway_session(db, "sess_ang", profile_name="ang")
            _create_gateway_session(db, "sess_ops", profile_name="ops")
            before_ang = _message_rows(db, "sess_ang")
            before_ops = _message_rows(db, "sess_ops")

            result = mirror_to_session(
                "telegram",
                "8244556262",
                "orphan default reminder",
                source_label="cron",
                user_id="8244556262",
                role="user",
                profile_name="default",
            )

            assert result is False
            assert _message_rows(db, "sess_ang") == before_ang
            assert _message_rows(db, "sess_ops") == before_ops
        finally:
            db.close()


class TestAppendToSqlite:
    def test_connection_is_closed_after_use(self, tmp_path):
        """Verify _append_to_sqlite closes the SessionDB connection."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()

        with patch("hermes_state.SessionDB", return_value=mock_db):
            _append_to_sqlite("sess_1", {"role": "assistant", "content": "hello"})

        mock_db.append_message.assert_called_once()
        mock_db.close.assert_called_once()

    def test_connection_closed_even_on_error(self, tmp_path):
        """Verify connection is closed even when append_message raises."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()
        mock_db.append_message.side_effect = Exception("db error")

        with patch("hermes_state.SessionDB", return_value=mock_db):
            _append_to_sqlite("sess_1", {"role": "assistant", "content": "hello"})

        mock_db.close.assert_called_once()
