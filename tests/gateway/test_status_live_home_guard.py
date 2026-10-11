"""Env-wiping tests must not reach the operator's live Hermes home.

``patch.dict(os.environ, {}, clear=True)`` drops ``HOME`` and ``HERMES_HOME``
together with every test signal (``PYTEST_*``, ``HERMES_TEST_ISOLATION``).
``gateway.status`` then fell back to ``Path.home()`` — which resolves through
the passwd entry when ``HOME`` is unset — and a feishu test wrote its own PID,
argv and code SHA into the live ``~/.hermes/gateway_state.json`` while the
real gateway was running.  The state.db guard had the same hole: its process
fallback walked only *parent* processes, and an in-process test's pytest is
the current process.

These tests stop at path resolution, so a regressed guard fails the
assertion without writing anything.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_state
import hermes_test_guard
from gateway import status


@pytest.fixture
def fresh_process_tree_memo(monkeypatch):
    monkeypatch.setattr(hermes_test_guard, "_PYTEST_IN_PROCESS_TREE", None)


def _live_root() -> Path:
    """Where a wiped-env process lands: ``Path.home()`` falls back to passwd."""
    with patch.dict(os.environ, {}, clear=True):
        return Path.home() / ".hermes"


class TestWipedEnvironmentInProcess:
    def test_gateway_identity_paths_refuse_the_live_home(self, fresh_process_tree_memo):
        with patch.dict(os.environ, {}, clear=True):
            for resolve in (
                status._get_pid_path,
                status._get_runtime_status_path,
                status._get_gateway_lock_path,
            ):
                with pytest.raises(RuntimeError, match="live-system guard"):
                    resolve()

    def test_state_db_guard_refuses_the_live_db(self, fresh_process_tree_memo):
        live_db = _live_root() / "state.db"
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="live-system guard"):
                hermes_state._ensure_test_isolation(live_db)

    def test_current_pytest_process_counts_as_test_context(self, fresh_process_tree_memo):
        with patch.dict(os.environ, {}, clear=True):
            assert hermes_test_guard.in_test_context()


class TestHermeticAndProductionPathsUnaffected:
    def test_hermetic_home_resolves_normally(self, tmp_path):
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            assert status._get_runtime_status_path() == tmp_path / "gateway_state.json"

    def test_profile_home_under_live_root_is_refused(self, fresh_process_tree_memo):
        profile = _live_root() / "profiles" / "coder"
        with patch.dict(os.environ, {"HERMES_HOME": str(profile)}, clear=True):
            with pytest.raises(RuntimeError, match="live-system guard"):
                status._get_pid_path()

    def test_outside_test_context_the_live_home_is_returned(self, monkeypatch):
        monkeypatch.setattr(hermes_test_guard, "in_test_context", lambda: False)
        with patch.dict(os.environ, {}, clear=True):
            assert status._get_pid_path().parent == _live_root()

    def test_bypass_marker_lets_a_deliberate_test_through(self, monkeypatch):
        monkeypatch.setattr(hermes_test_guard, "BYPASS", True)
        with patch.dict(os.environ, {}, clear=True):
            assert status._get_pid_path().parent == _live_root()
