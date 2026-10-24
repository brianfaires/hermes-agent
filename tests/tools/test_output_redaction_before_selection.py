"""Synthetic credentials are masked with their full context before display cuts."""
import json
import shlex
import time

import pytest

import hermes_bootstrap
from agent import redact
from tools import process_registry as pr
from tools.registry import registry as tool_registry
from tools.environments.local import LocalEnvironment
from tests.tools.test_terminal_error_redaction import _patch_common

SECRET = 'opaque-credential-fragment-' * 120


@pytest.mark.parametrize('line', [
    'API_KEY="' + SECRET + ' has spaces"',
    "E       assert {'API_KEY': '" + SECRET + "'}",
    'RuntimeError: API_KEY="' + SECRET + ' has spaces"',
    "RuntimeError: {'API_KEY': '" + SECRET,
], ids=['quoted-env', 'pytest-repr', 'exception-assignment', 'partial-repr'])
def test_foreground_redacts_full_assignments_before_final_cut(monkeypatch, line):
    class Environment:
        env = {}
        def execute(self, command, **kwargs):
            return {'output': 'HEAD\n' + line + '\nTAIL', 'returncode': 0}
    _patch_common(monkeypatch, Environment())
    monkeypatch.setattr('tools.tool_output_limits.get_max_bytes', lambda: 180)
    monkeypatch.setattr(redact, '_REDACT_ENABLED', True)
    result = json.loads(tool_registry.dispatch('terminal', {'command': 'printenv'}))
    assert 'credential-fragment' not in result['output']
    assert '***' in result['output']
    assert len(result['output']) <= 180


def test_status_suffix_masks_complete_capture_before_reserving_its_budget(monkeypatch):
    from tools.environments.base_output import _BoundedOutputCollector, _finalize_wait_result

    class Environment:
        env = {}

        def execute(self, command, **kwargs):
            collector = _BoundedOutputCollector(200)
            collector.redaction_command = command
            collector.append('API_KEY="' + 'opaque-secret-' * 13 + '"\n')
            return _finalize_wait_result(collector, collector.render(suffix='\n[Command interrupted]'), 130)

    _patch_common(monkeypatch, Environment())
    monkeypatch.setattr('tools.tool_output_limits.get_max_bytes', lambda: 200)
    monkeypatch.setattr(redact, '_REDACT_ENABLED', True)
    result = json.loads(tool_registry.dispatch('terminal', {'command': 'printenv'}))
    assert 'opaque-secret' not in result['output']
    assert '[Command interrupted]' in result['output']
    assert len(result['output']) <= 200


@pytest.mark.platforms('posix')
def test_real_capture_redacts_spill_before_bounding_hook_input(tmp_path, monkeypatch):
    environment = LocalEnvironment(cwd=str(tmp_path), timeout=10)
    _patch_common(monkeypatch, environment)
    monkeypatch.setattr('tools.tool_output_limits.get_max_bytes', lambda: 300)
    monkeypatch.setattr(redact, '_REDACT_ENABLED', True)
    inputs = []
    def hook(name, **kwargs):
        if name == 'transform_terminal_output':
            inputs.append(kwargs['output'])
        return []
    monkeypatch.setattr('hermes_cli.plugins.invoke_hook', hook)
    script = "print('HEAD'); print(\"RuntimeError: {'API_KEY': '\" + 'opaque-credential-fragment-' * 300 + \"'}\"); print('TAIL')"
    command = shlex.join(['python3', '-c', script])
    try:
        result = json.loads(tool_registry.dispatch('terminal', {'command': command}))
    finally:
        environment.cleanup()
    assert len(inputs) == 1 and len(inputs[0]) <= 300
    assert 'credential-fragment' not in inputs[0]
    assert 'credential-fragment' not in result['output']
    assert 'HEAD' in result['output'] and 'TAIL' in result['output']


@pytest.mark.parametrize('command,line', [
    ('printenv', 'API_KEY="' + SECRET + ' has spaces"'),
    ('python -m pytest', "E       assert {'API_KEY': '" + SECRET + "'}"),
])
def test_background_previews_logs_and_retention_redact_before_selection(monkeypatch, command, line):
    monkeypatch.setattr(redact, '_REDACT_ENABLED', True)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    session = pr.ProcessSession(id='proc_synthetic', command=command, started_at=time.time(),
                                output_buffer='HEAD\n' + line + '\nTAIL')
    registry._running[session.id] = session
    for action in ('poll', 'log'):
        result = json.loads(tool_registry.dispatch('process_manage', {'action': action, 'session_id': session.id, 'limit': 1}))
        assert 'credential-fragment' not in result.get('output', '') + result.get('output_preview', '')
    # Once capture evicts the start of a line, its fragment is never displayed.
    session.output_buffer = ''
    session.max_output_chars = 160
    session.append_output('HEAD\n' + line)
    result = json.loads(tool_registry.dispatch('process_manage', {'action': 'poll', 'session_id': session.id}))
    assert 'credential-fragment' not in result['output_preview']
    session.append_output('\nuseful next line\n')
    result = json.loads(tool_registry.dispatch('process_manage', {'action': 'poll', 'session_id': session.id}))
    assert 'useful next line' in result['output_preview']
    assert 'credential-fragment' not in result['output_preview']
