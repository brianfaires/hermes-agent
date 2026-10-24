"""Gateway tails keep the unconditional privacy floor before any cut."""
import pytest

from agent import redact
from gateway.run import GatewayRunner
from tools.process_registry import ProcessSession


@pytest.mark.parametrize('enabled', [True, False])
def test_gateway_partial_diagnostic_masks_before_tail(monkeypatch, enabled):
    monkeypatch.setattr(redact, '_REDACT_ENABLED', enabled)
    session = ProcessSession(id='proc_gateway', command='python -m pytest', exit_code=1,
                             output_buffer="RuntimeError: {'API_KEY': '" + 'opaque-secret-fragment-' * 120)
    output = GatewayRunner._redacted_output_tail(session, 120)
    assert 'opaque-secret-fragment' not in output


@pytest.mark.parametrize('enabled', [True, False])
def test_gateway_completion_uses_retention_context(monkeypatch, enabled):
    monkeypatch.setattr(redact, '_REDACT_ENABLED', enabled)
    session = ProcessSession(id='proc_gateway', command='python -m pytest', exit_code=1,
                             max_output_chars=140)
    session.append_output("RuntimeError: {'API_KEY': '" + 'opaque-secret-fragment-' * 120)
    event = GatewayRunner._build_process_completion_event({}, session, session.id)
    assert 'opaque-secret-fragment' not in event['output']
