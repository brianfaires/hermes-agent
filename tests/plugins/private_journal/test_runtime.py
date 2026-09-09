import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from plugins.private_journal import capture, processor, memory, retention, runtime, storage


@pytest.fixture
def batch(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    vault = tmp_path / 'vault'
    monkeypatch.setenv('HERMES_HOME', str(home))
    ack = capture.capture_log('  author saw X; Y was reported. Maybe yesterday.\n ')
    entry_id = ack.split()[1]
    processor.process_pending(vault_path=vault, llm_call=lambda **kw: json.dumps({'entries': [{'id': entry_id}]}))
    return home, vault, entry_id


def test_finalized_ingested_once_and_raw_duplicate_cleanup(batch):
    home, vault, entry_id = batch
    transport = Mock(return_value=SimpleNamespace(success=True, operation_id=None))
    memory.ingest_finalized(vault, 'fixture-bank', transport)
    memory.ingest_finalized(vault, 'fixture-bank', transport)
    transport.assert_called_once()
    kw = transport.call_args.kwargs
    assert kw['document_id'] == memory.identity('fixture-bank', entry_id)
    assert kw['bank_id'] == 'fixture-bank' and kw['retain_async'] is False
    assert isinstance(kw['timestamp'], datetime)
    assert processor.extract_raw_section(kw['content']).startswith('  author')
    assert 'Capture date is not event date' in kw['context']
    raw = home / 'journal' / 'holding' / f'{entry_id}.json'
    archive = home / 'journal' / 'archive' / raw.name
    original = raw.read_bytes()
    assert archive.read_bytes() == original
    assert retention.cleanup(vault_path=vault) == [entry_id]
    assert raw.read_bytes() == original
    assert retention.cleanup(vault_path=vault, delete_holding_duplicates=True) == [entry_id]
    assert not raw.exists() and archive.read_bytes() == original
    assert processor.process_pending(vault_path=vault, llm_call=Mock(side_effect=AssertionError())).status == 'empty'
    memory.ingest_finalized(vault, 'fixture-bank', transport)
    transport.assert_called_once()


@pytest.mark.parametrize('failure', ['exception', 'negative', 'receipt_crash'])
def test_memory_uncertainty_never_blind_retries(batch, monkeypatch, failure):
    home, vault, entry_id = batch
    transport = Mock(return_value=SimpleNamespace(success=True, operation_id=None))
    if failure == 'exception':
        transport.side_effect = TimeoutError('PRIVATE transport payload')
    elif failure == 'negative':
        transport.return_value = SimpleNamespace(success=False)
    else:
        publish = storage.publish
        def fail_receipt(path, *a, **kw):
            if path.parent.name == 'receipts':
                raise OSError('receipt crash')
            return publish(path, *a, **kw)
        monkeypatch.setattr(storage, 'publish', fail_receipt)
    with pytest.raises((ValueError, OSError)):
        memory.ingest_finalized(vault, 'fixture-bank', transport)
    assert list(vault.glob('entries/*/*/*/*.md'))
    with pytest.raises(ValueError, match='uncertain'):
        memory.ingest_finalized(vault, 'fixture-bank', transport)
    assert transport.call_count == 1


def test_explicit_reconciliation_and_profile_identity(batch, monkeypatch, tmp_path):
    home, vault, entry_id = batch
    transport = Mock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        memory.ingest_finalized(vault, 'bank', transport)
    with pytest.raises(ValueError):
        memory.reconcile(entry_id, bank='different', outcome='present')
    memory.reconcile(entry_id, bank='bank', outcome='present')
    memory.ingest_finalized(vault, 'bank', transport)
    transport.assert_called_once()
    before = memory.identity('bank', entry_id)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other'))
    assert memory.identity('bank', entry_id) != before


def test_archive_tampering_prevents_cleanup(batch):
    home, vault, entry_id = batch
    archive = home / 'journal' / 'archive' / f'{entry_id}.json'
    archive.write_text('{}')
    with pytest.raises((ValueError, processor.ProcessorError)):
        retention.cleanup(vault_path=vault, delete_holding_duplicates=True)
    assert (home / 'journal' / 'holding' / archive.name).exists()


def test_real_model_resolver_transport_boundary(monkeypatch):
    import agent.auxiliary_client as auxiliary
    monkeypatch.setattr(runtime, 'settings', lambda: {'provider': 'openrouter', 'model': 'fixture/inexpensive:free'})
    client = Mock()
    client.with_options.return_value = client
    client.chat.completions.create.return_value = 'response'
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-not-a-real-key')
    factory = Mock(return_value=client)
    monkeypatch.setattr(auxiliary, '_create_openai_client', factory)
    assert runtime.call_model(
        messages=[{'role': 'user', 'content': 'fixture'}],
        max_tokens=20,
        timeout=2,
        task='private_journal_batch',
        response_format={'type': 'json_object'},
        reasoning={'effort': 'low', 'exclude': True},
    ) == 'response'
    factory.assert_called_once()
    assert client.chat.completions.create.call_args.kwargs['model'] == 'fixture/inexpensive:free'
    assert client.chat.completions.create.call_args.kwargs['response_format'] == {'type': 'json_object'}
    assert client.chat.completions.create.call_args.kwargs['extra_body'] == {'reasoning': {'effort': 'low', 'exclude': True}}
    client.with_options.assert_called_once_with(max_retries=0, timeout=2)
    assert 'tools' not in client.chat.completions.create.call_args.kwargs
    client.close.assert_called_once()


def test_openrouter_options_pass_through_real_openai_sdk(monkeypatch):
    import httpx
    from openai import OpenAI
    import agent.auxiliary_client as auxiliary

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id': 'fictional-sdk-test', 'object': 'chat.completion', 'created': 0,
            'model': 'openai/gpt-oss-20b',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': '{"entries":[]}'}}],
        })

    client = OpenAI(api_key='fictional-sdk-test', base_url='https://fixture.invalid/v1',
                    http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(runtime, 'settings', lambda: {
        'provider': 'openrouter', 'model': 'openai/gpt-oss-20b'})
    monkeypatch.setattr(auxiliary, 'resolve_provider_client',
                        lambda **kwargs: (client, 'openai/gpt-oss-20b'))
    response = runtime.call_model(
        messages=[{'role': 'user', 'content': 'FICTIONAL JSON serialization test'}],
        max_tokens=4096, timeout=2, task='private_journal_batch',
        response_format={'type': 'json_object'},
        reasoning={'effort': 'low', 'exclude': True},
    )
    assert response.choices[0].message.content == '{"entries":[]}'
    assert len(requests) == 1
    assert requests[0]['response_format'] == {'type': 'json_object'}
    assert requests[0]['reasoning'] == {'effort': 'low', 'exclude': True}
    assert requests[0]['max_tokens'] == 4096
    assert client.is_closed()


def test_real_hindsight_sdk_boundary(monkeypatch):
    import hindsight_client
    monkeypatch.setattr(runtime, 'settings', lambda: {'hindsight_url': 'http://fixture.invalid'})
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(hindsight_client, 'Hindsight', factory)
    runtime.retain_finalized(bank_id='fixture', content='final', document_id='stable')
    client.retain.assert_called_once_with(bank_id='fixture', content='final', document_id='stable')
    client.close.assert_called_once()


def test_runtime_run_reaches_finalized_memory_transport(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    entry_id = capture.capture_log('fixture only').split()[1]
    schema = tmp_path / 'schema'
    (schema / 'templates').mkdir(parents=True)
    for relative in ('personal-history-log.md', 'data-dictionary.md', 'templates/entry-template.md'):
        (schema / relative).write_text('fixture schema')
    monkeypatch.setattr(runtime, 'settings', lambda: dict(batch_enabled=True, model='fixture/model', bank_id='fixture', vault_path=str(tmp_path / 'vault'), schema_root=str(schema)))
    llm = Mock(return_value=json.dumps({'entries': [{'id': entry_id}]}))
    retain = Mock(return_value=SimpleNamespace(success=True, operation_id=None))
    monkeypatch.setattr(runtime, 'call_model', llm)
    monkeypatch.setattr(runtime, 'retain_finalized', retain)
    runtime.run_batch()
    runtime.run_batch()
    llm.assert_called_once()
    retain.assert_called_once()
    assert 'fixture schema' in llm.call_args.kwargs['messages'][1]['content']


def test_symlink_components_and_lock_fail_closed(tmp_path, monkeypatch):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv('HERMES_HOME', str(link / 'home'))
    with pytest.raises(capture.PrivateJournalStoreError):
        capture.capture_log('fixture')
    assert list(real.iterdir()) == []
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    holding = capture.holding_dir()
    target = tmp_path / 'target'
    target.write_text('unchanged')
    (holding / 'processor.lock').symlink_to(target)
    with pytest.raises(OSError):
        with processor._process_lock():
            pytest.fail('lock followed link')
    assert target.read_text() == 'unchanged'


def test_structured_basis_cannot_be_laundered():
    entry = capture.capture_record('fixture')['id']
    with pytest.raises(processor.ProcessorError, match='contradicts'):
        processor._validate_extraction({'id': entry, 'direct_observations': [{'text': 'reported', 'basis': 'reported', 'confidence': 'low'}]})


def test_real_sdk_builds_one_document_request(batch, monkeypatch):
    from hindsight_client_api.api.memory_api import MemoryApi
    home, vault, entry_id = batch
    monkeypatch.setattr(runtime, 'settings', lambda: {'hindsight_url': 'http://fixture.invalid'})
    calls = []
    async def transport(self, bank_id, request, **kwargs):
        calls.append((bank_id, request, kwargs))
        return SimpleNamespace(success=True, operation_id=None)
    monkeypatch.setattr(MemoryApi, 'retain_memories', transport)
    memory.ingest_finalized(vault, 'fixture', runtime.retain_finalized)
    assert len(calls) == 1
    bank, request, kwargs = calls[0]
    assert bank == 'fixture' and request.var_async is False
    assert len(request.items) == 1
    assert request.items[0].document_id == memory.identity(bank, entry_id)
    assert request.items[0].metadata['source'] == 'private-journal'
    assert request.items[0].timestamp is not None


def test_script_failure_is_sanitized_deduped_and_nonzero(tmp_path, monkeypatch, capsys):
    from plugins.private_journal.scripts.process_private_journal import main
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr('sys.argv', ['processor', '--home', str(home)])
    monkeypatch.setattr(runtime, 'run_batch', Mock(side_effect=ValueError('PRIVATE_SENTINEL')))
    assert main() == 1
    out = capsys.readouterr()
    assert not out.out and 'PRIVATE_SENTINEL' not in out.err
    assert len(out.err) <= 800 and len(out.err.splitlines()) <= 6
    assert main() == 1
    assert capsys.readouterr().err == ''
    monkeypatch.setattr(runtime, 'run_batch', lambda: None)
    assert main() == 0
    assert not capsys.readouterr().out


def test_reconcile_absent_permits_one_new_attempt(batch):
    home, vault, entry_id = batch
    with pytest.raises(TimeoutError):
        memory.ingest_finalized(vault, 'fixture', Mock(side_effect=TimeoutError()))
    memory.reconcile(entry_id, bank='fixture', outcome='absent')
    transport = Mock(return_value=SimpleNamespace(success=True, operation_id=None))
    memory.ingest_finalized(vault, 'fixture', transport)
    memory.ingest_finalized(vault, 'fixture', transport)
    transport.assert_called_once()
    assert list((home / 'journal' / 'memory' / 'reconciliations').glob('*.json'))


def test_crash_after_link_recovers_but_external_hardlink_rejected(tmp_path):
    import os
    target = tmp_path / 'private'
    storage.publish(target, b'fixture')
    tmp = tmp_path / ('.' + 'a' * 32 + '.tmp')
    os.link(target, tmp)
    assert storage.read(target) == b'fixture'
    assert not tmp.exists()
    external = tmp_path / 'external'
    os.link(target, external)
    with pytest.raises(ValueError, match='unsafe'):
        storage.read(target)
