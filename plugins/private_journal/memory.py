"""Finalized-only ingestion, with durable ambiguity barriers and reconciliation.

Hindsight document_id groups memories; replace/append is NOT a transaction
receipt. Never retry an unresolved intent automatically, even on an exception.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from hermes_constants import get_hermes_home
from . import storage
from .capture import holding_dir, validate_entry_id


def _bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n').encode()


def identity(bank, entry_id, *, scope='final'):
    if not isinstance(bank, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', bank):
        raise ValueError('explicit bank required')
    profile_scope = hashlib.sha256(str(Path(get_hermes_home())).encode()).hexdigest()[:24]
    if scope == 'final':
        return f'private-journal-{profile_scope}-{bank}-{validate_entry_id(entry_id)}'
    if scope == 'raw':
        return f'private-journal-raw-{profile_scope}-{bank}-{validate_entry_id(entry_id)}'
    raise ValueError('invalid memory identity scope')


def _paths(entry_id, *, scope='final'):
    root = holding_dir().parent / 'memory'
    if scope == 'final':
        return root / 'intents' / f'{validate_entry_id(entry_id)}.json', root / 'receipts' / f'{entry_id}.json'
    if scope == 'raw':
        return root / 'raw-intents' / f'{validate_entry_id(entry_id)}.json', root / 'raw-receipts' / f'{entry_id}.json'
    raise ValueError('invalid memory path scope')


def _record_policy(record):
    policy = record.get('memory_retention')
    if not isinstance(policy, dict) or policy.get('schema_version') != 1:
        return {'schema_version': 1, 'enabled': False}
    if policy.get('enabled') is not True or policy.get('provider') != 'hindsight':
        return {'schema_version': 1, 'enabled': False}
    return {
        'schema_version': 1,
        'enabled': True,
        'provider': 'hindsight',
        'bank_id': policy.get('bank_id') if isinstance(policy.get('bank_id'), str) else None,
    }


def _raw_payload(record, bank):
    from .processor import _sha256

    policy = _record_policy(record)
    content = str(record['text']).encode('utf-8')
    return dict(
        schema_version=1,
        retention_kind='raw',
        entry_id=record['id'],
        document_id=identity(bank, record['id'], scope='raw'),
        bank_id=bank,
        provider=policy.get('provider'),
        policy=policy,
        profile=str(Path(get_hermes_home())),
        captured_at=record['captured_at'],
        source=record.get('source') or {},
        sha256=_sha256(content),
    )


def _metadata_source(source):
    if not isinstance(source, dict):
        source = {}
    flat = {str(key): str(value) for key, value in source.items()}
    return json.dumps(flat, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _assert_sync_success(response, bank):
    if (getattr(response, 'success', None) is not True
            or getattr(response, 'operation_id', None)
            or getattr(response, 'operation_ids', None)
            or getattr(response, 'var_async', False) is True
            or getattr(response, 'bank_id', bank) != bank):
        raise ValueError('memory outcome uncertain; reconcile explicitly')


def ingest_opted_in_raw(vault, bank, transport=None):
    from .processor import _all_records
    from .runtime import retain_memory, memory_retention_policy

    active_policy = memory_retention_policy()
    if active_policy.get('enabled') is not True:
        return
    for record in _all_records():
        policy = _record_policy(record)
        if policy.get('enabled') is not True:
            continue
        if policy.get('provider') != active_policy.get('provider'):
            continue
        if policy.get('bank_id') not in {None, bank}:
            raise ValueError('memory retention bank mismatch')
        content = str(record['text'])
        payload = _raw_payload(record, bank)
        data = _bytes(payload)
        intent, receipt = _paths(record['id'], scope='raw')
        if receipt.exists():
            if storage.read(receipt) != data or storage.read(intent) != data:
                raise ValueError('memory receipt identity mismatch')
            continue
        if intent.exists():
            raise ValueError('memory outcome uncertain; reconcile explicitly')
        storage.publish(intent, data, verify=False)
        response = (transport or retain_memory)(
            provider=policy['provider'],
            bank_id=bank,
            content=content,
            document_id=payload['document_id'],
            timestamp=datetime.fromisoformat(record['captured_at']),
            context='Verbatim original private journal /log text, attributed to its author. Capture date is not event date. Preserve uncertainty; do not infer beyond the original wording.',
            metadata={
                'source': 'private-journal',
                'retention_kind': 'raw',
                'entry_id': record['id'],
                'profile_id': payload['document_id'].split('-')[3],
                'captured_at': record['captured_at'],
                'raw_sha256': payload['sha256'],
                'capture_source': _metadata_source(record.get('source')),
            },
            retain_async=False,
        )
        _assert_sync_success(response, bank)
        storage.publish(receipt, data, verify=False)


def ingest_finalized(vault, bank, transport=None):
    from .processor import _all_records, _validate_receipt, output_path, _sha256
    from .runtime import retain_finalized, settings
    for record in _all_records():
        if not _validate_receipt(record, vault):
            continue
        path = output_path(vault, record)
        content = storage.read(path)
        intent, receipt = _paths(record['id'])
        payload = dict(entry_id=record['id'], document_id=identity(bank, record['id']),
                       bank_id=bank, endpoint=settings().get('hindsight_url'),
                       profile=str(Path(get_hermes_home())),
                       date=record['captured_at'], sha256=_sha256(content))
        data = _bytes(payload)
        if receipt.exists():
            if storage.read(receipt) != data or storage.read(intent) != data:
                raise ValueError('memory receipt identity mismatch')
            continue
        if intent.exists():
            raise ValueError('memory outcome uncertain; reconcile explicitly')
        storage.publish(intent, data, verify=False)
        # The FINALIZED entry alone is sent. Its raw section and deterministic
        # extraction travel as one authoritative document, never separate retains.
        response = (transport or retain_finalized)(
            bank_id=bank, content=content.decode('utf-8'),
            document_id=payload['document_id'], timestamp=datetime.fromisoformat(record['captured_at']),
            context='Authoritative journal account, attributed to its author. Capture date is not event date. Preserve reported/inferred/unknown distinctions; do not strengthen claims.',
            metadata={'source': 'private-journal', 'entry_id': record['id'],
                      'profile_id': payload['document_id'].split('-')[2],
                      'journal_sha256': payload['sha256']},
            retain_async=False,
        )
        _assert_sync_success(response, bank)
        storage.publish(receipt, data, verify=False)


def reconcile(entry_id, *, bank, outcome):
    """Operator has inspected the explicit remote bank/document before calling.

    present: certify matching finalized document as retained. absent: certify
    remote operation terminated without storing anything; permit one new attempt.
    No automatic lookup or blind retry; evidence and decision remain operator-owned.
    """
    from .processor import _process_lock
    with _process_lock():
        intent, receipt = _paths(entry_id, scope='raw')
        if not intent.exists():
            intent, receipt = _paths(entry_id)
        data = storage.read(intent)
        payload = json.loads(data)
        scope = payload.get('retention_kind', 'final')
        if payload['document_id'] != identity(bank, entry_id, scope=scope) or payload['bank_id'] != bank:
            raise ValueError('reconciliation identity mismatch')
        if outcome == 'present':
            storage.publish(receipt, data)
        elif outcome == 'absent' and not receipt.exists():
            audit = intent.parent.parent / 'reconciliations' / f'{entry_id}-{__import__("secrets").token_hex(6)}.json'
            storage.publish(audit, data, verify=False)
            storage.remove_verified(intent, data)
        else:
            raise ValueError('invalid reconciliation outcome')
