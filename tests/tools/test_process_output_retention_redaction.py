"""Retention eviction and asynchronous notices keep redaction context."""
import json
import time

from agent import redact
from tools import process_registry as pr
from tools.environments.base_output import _BoundedOutputCollector, _finalize_wait_result
from tools.registry import registry as tool_registry


def _session(monkeypatch, **kwargs):
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    monkeypatch.setattr(redact, '_REDACT_ENABLED', True)
    session = pr.ProcessSession(id='proc_safe', command='python -m pytest',
                                started_at=time.time(), **kwargs)
    registry._running[session.id] = session
    return registry, session


def _preview(session):
    return json.loads(tool_registry.dispatch('process_manage', {'action': 'poll', 'session_id': session.id}))['output_preview']


def test_pem_prefix_survives_eviction_and_later_output_stays_useful(monkeypatch):
    _, session = _session(monkeypatch, max_output_chars=140)
    session.append_output('-----BEGIN PRIVATE KEY-----\n' + 'SyntheticKeyMaterial\n' * 30)
    assert 'SyntheticKeyMaterial' not in _preview(session)
    session.append_output('-----END PRIVATE KEY-----\nuseful completion\n')
    output = _preview(session)
    assert 'SyntheticKeyMaterial' not in output
    assert 'useful completion' in output


def test_evicted_end_marker_keeps_later_output_useful(monkeypatch):
    _, session = _session(monkeypatch, max_output_chars=80)
    session.append_output('-----BEGIN PRIVATE KEY-----\n' + 'SyntheticKeyMaterial\n' * 30)
    session.append_output('-----END PRIVATE KEY-----\nuseful completion\n' + 'x' * 50)
    output = _preview(session)
    assert 'SyntheticKeyMaterial' not in output
    assert 'useful completion' in output


def test_watch_partial_chunk_and_heartbeat_use_complete_retained_context(monkeypatch):
    registry, session = _session(monkeypatch, watch_patterns=['opaque-secret-fragment'], heartbeat_seconds=60)
    secret = 'opaque-secret-fragment-' * 200
    session.append_output("E       assert {'API_KEY': '" + secret + "'}\n")
    registry._check_watch_patterns(session, secret[-180:] + "'}\n")
    notice = registry.completion_queue.get_nowait()
    assert notice['type'] == 'watch_match'
    assert 'opaque-secret-fragment' not in notice['output']
    registry._emit_heartbeat(session, time.time())
    notice = registry.completion_queue.get_nowait()
    assert notice['type'] == 'heartbeat'
    assert 'opaque-secret-fragment' not in notice['output']


def test_incomplete_capped_spill_fails_closed_with_bounded_status_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(redact, '_REDACT_ENABLED', True)
    collector = _BoundedOutputCollector(120, spill_path=tmp_path / 'private' / 'output.log')
    collector.redaction_command = 'printenv'
    collector._SPILL_CAP_CHARS = 200
    collector.append('API_KEY="' + 'opaque-fragment-' * 100 + '"')
    result = _finalize_wait_result(collector, collector.render(suffix='\n[Command interrupted]'), 130)
    assert 'opaque-fragment' not in result['output']
    assert 'redaction context' in result['output']
    assert result['output'].endswith('[Command interrupted]')
    assert len(result['output']) <= 120
