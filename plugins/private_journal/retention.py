"""Only verified duplicate holding records can be deleted; default is dry run."""
from . import storage
from .capture import holding_dir
from .processor import _process_lock, _all_records, _validate_receipt, _resolve_vault_path


def cleanup(*, vault_path, delete_holding_duplicates=False):
    with _process_lock():
        vault = _resolve_vault_path(vault_path)
        candidates = []
        for record in _all_records():
            raw = holding_dir() / f"{record['id']}.json"
            if not raw.exists() or not _validate_receipt(record, vault):
                continue
            archive = holding_dir().parent / 'archive' / raw.name
            data = storage.read(raw)
            if storage.read(archive) != data:
                raise ValueError('archive verification failed')
            candidates.append((raw, data))
        if delete_holding_duplicates:
            for path, data in candidates:
                storage.remove_verified(path, data)
        return [p.stem for p, _ in candidates]
