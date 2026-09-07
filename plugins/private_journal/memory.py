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


def identity(bank, entry_id):
    if not isinstance(bank, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', bank):
        raise ValueError('explicit bank required')
    scope = hashlib.sha256(str(Path(get_hermes_home())).encode()).hexdigest()[:24]
    return f'private-journal-{scope}-{bank}-{validate_entry_id(entry_id)}'


def _paths(entry_id):
    root = holding_dir().parent / 'memory'
    return root / 'intents' / f'{validate_entry_id(entry_id)}.json', root / 'receipts' / f'{entry_id}.json'


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
        if (getattr(response, 'success', None) is not True
                or getattr(response, 'operation_id', None)
                or getattr(response, 'operation_ids', None)
                or getattr(response, 'var_async', False) is True
                or getattr(response, 'bank_id', bank) != bank):
            raise ValueError('memory outcome uncertain; reconcile explicitly')
        storage.publish(receipt, data, verify=False)


def reconcile(entry_id, *, bank, outcome):
    """Operator has inspected the explicit remote bank/document before calling.

    present: certify matching finalized document as retained. absent: certify
    remote operation terminated without storing anything; permit one new attempt.
    No automatic lookup or blind retry; evidence and decision remain operator-owned.
    """
    from .processor import _process_lock
    with _process_lock():
        intent, receipt = _paths(entry_id)
        data = storage.read(intent)
        payload = json.loads(data)
        if payload['document_id'] != identity(bank, entry_id) or payload['bank_id'] != bank:
            raise ValueError('reconciliation identity mismatch')
        if outcome == 'present':
            storage.publish(receipt, data)
        elif outcome == 'absent' and not receipt.exists():
            audit = intent.parent.parent / 'reconciliations' / f'{entry_id}-{__import__("secrets").token_hex(6)}.json'
            storage.publish(audit, data, verify=False)
            storage.remove_verified(intent, data)
        else:
            raise ValueError('invalid reconciliation outcome')
