"""Non-owner execution must not use a parent worker's lifecycle identity."""
import os
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
from agent.kanban_stop import build_kanban_stop_nudge
from agent.turn_finalizer import finalize_turn
from tools import kanban_tools as kt


class _LimitAgent:
    def __init__(
        self,
        *,
        max_iterations=60,
        budget_remaining=0,
        completion_explainer=False,
    ):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=budget_remaining, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        self._handle_max_iterations_called = False
        self._completion_explainer = completion_explainer

    def _handle_max_iterations(self, messages, api_call_count):
        self._handle_max_iterations_called = True
        return "summary from extra call"

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return self._completion_explainer

    def _format_turn_completion_explanation(self, _reason):
        return "iteration-limit explanation"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


class WorkerScopeTests(unittest.TestCase):
    def test_child_does_not_offer_unavailable_kanban_skills(self):
        from agent.skill_utils import _detect_environment
        with patch.object(kt, '_profile_has_kanban_toolset', return_value=True):
            self.assertTrue(_detect_environment('kanban'))
            with delegated_child_context():
                self.assertFalse(_detect_environment('kanban'))
                self.assertFalse(kt._check_kanban_mode())
            self.assertTrue(_detect_environment('kanban'))

    def test_nonowners_skip_nudge_and_restore_parent(self):
        with patch.dict(os.environ, {'HERMES_KANBAN_TASK': 'parent', 'HERMES_KANBAN_STOP_NUDGE': '1'}):
            for scope in (delegated_child_context, non_dispatcher_owned_context):
                with scope():
                    self.assertIsNone(build_kanban_stop_nudge(messages=[]))
                self.assertIn('parent', build_kanban_stop_nudge(messages=[]))

    def test_nonowners_do_not_poll_parent_or_consume_throttles(self):
        with patch.dict(os.environ, {'HERMES_KANBAN_TASK': 'parent'}):
            for scope in (delegated_child_context, non_dispatcher_owned_context):
                with patch.object(kt, '_connect') as connect, \
                     patch.object(kt, '_auto_heartbeat_last_attempt', 0), \
                     patch.object(kt, '_comment_poll_last_attempt', 0), scope():
                    self.assertFalse(kt.heartbeat_current_worker_from_env())
                    self.assertFalse(kt.inject_new_comments_from_env(Mock()))
                    connect.assert_not_called()
                    self.assertEqual(kt._auto_heartbeat_last_attempt, 0)
                    self.assertEqual(kt._comment_poll_last_attempt, 0)

    def test_nonowners_skip_budget_lifecycle_before_database_open(self):
        for scope in (delegated_child_context, non_dispatcher_owned_context):
            with patch('hermes_cli.kanban_db.connect') as connect, scope():
                with patch.dict(os.environ, {'HERMES_KANBAN_TASK': 'parent'}), \
                     patch('hermes_cli.plugins.invoke_hook', return_value=[]):
                    result = finalize_turn(
                        _LimitAgent(), final_response=None, api_call_count=60,
                        interrupted=False, failed=False,
                        messages=[{'role': 'user', 'content': 'child task'}],
                        conversation_history=[], effective_task_id='child',
                        turn_id='turn', user_message='child task',
                        original_user_message='child task', _should_review_memory=False,
                        _turn_exit_reason='unknown',
                    )
                    self.assertEqual(result['final_response'], 'summary from extra call')
                connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
