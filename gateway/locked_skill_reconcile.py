"""Owner Git bookkeeping after exact apply; never checks out live files.

Git's index.lock is held from revalidation through replacement/ref CAS/receipt.
A temporary native Git index changes only the approved entry. Durable exact index
hashes fence recovery; unknown/stale Git locks and concurrent drift fail closed.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid

from gateway.locked_skill_apply import _file_identity, _load, mutation_scope
from gateway.locked_skill_proposals import _read, _require, _save
from hermes_cli.locked_skill_proposals import digest, git, regular


@contextmanager
def _index_owner(manifest):
    index = Path(git(manifest, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
    regular(index)
    lock = index.with_name(index.name + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    owned = os.fstat(fd)
    try:
        yield index
    finally:
        os.close(fd)
        current = lock.lstat()
        _require((current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino), 'index_lock_changed')
        lock.unlink()


def _other_entries(data, path):
    return [entry for entry in data.split(b'\0') if entry and entry.partition(b'\t')[2] != path.encode()]


def _prepare_index(manifest, index, temporary):
    before = regular(index)
    mode = stat.S_IMODE(index.stat().st_mode)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(before)
    git(manifest, 'update-index', '--cacheinfo', manifest['mode'], manifest['post_blob'], manifest['path'],
        index_file=temporary)
    synced = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(synced, mode)
        os.fsync(synced)
    finally:
        os.close(synced)
    after = regular(temporary)
    original_entries = git(manifest, 'ls-files', '--stage', '-z')
    candidate_entries = git(manifest, 'ls-files', '--stage', '-z', index_file=temporary)
    _require(_other_entries(original_entries, manifest['path']) == _other_entries(candidate_entries, manifest['path']),
             'unrelated_index_changed')
    return before, after


def _reconcile_owned(service, fingerprint, manifest, index, applied):
    _load(service, fingerprint)  # Revalidate under Git's index ownership, not before it.
    target = Path(manifest['home']) / manifest['path']
    _require(regular(target) == bytes.fromhex(manifest['postimage_hex'])
             and _file_identity(target.stat()) == applied['post_identity'], 'target_changed')
    _require(git(manifest, 'symbolic-ref', 'HEAD').strip() == b'refs/heads/main', 'unsupported_route')
    with service.db._read_ctx() as conn:
        previous = _read(conn, 'reconciliation:' + fingerprint)
    if previous is not None and previous['completed']:
        return {**previous, 'already_reconciled': True}
    current_index = regular(index)
    if previous is not None:
        _require(digest(current_index) in {previous['pre_index_sha256'], previous['post_index_sha256']}, 'index_changed')
    temporary = index.with_name('locked-proposal-index-' + uuid.uuid4().hex)
    try:
        before, after = _prepare_index(manifest, index, temporary)
        receipt = previous or {'manifest_sha256': fingerprint, 'head': manifest['head'], 'completed': False,
                               'pre_index_sha256': digest(before), 'post_index_sha256': digest(after)}
        _require(digest(after) == receipt['post_index_sha256'], 'index_changed')
        service.db._execute_write(lambda conn: _save(conn, 'reconciliation:' + fingerprint, receipt))
        _require(regular(index) == before, 'index_changed')
        os.replace(temporary, index)  # index.lock remains held at its separate sentinel path.
        directory = os.open(index.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        head = git(manifest, 'rev-parse', 'HEAD').decode().strip()
        if head != manifest['head']:
            _require(head == manifest['base'], 'target_changed')
            git(manifest, 'update-ref', 'refs/heads/main', manifest['head'], manifest['base'])
        _load(service, fingerprint)
        _require(regular(index) == after and regular(target) == bytes.fromhex(manifest['postimage_hex'])
                 and _file_identity(target.stat()) == applied['post_identity'], 'target_changed')
        receipt['completed'] = True
        service.db._execute_write(lambda conn: _save(conn, 'reconciliation:' + fingerprint, receipt))
        return receipt
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def reconcile(service, fingerprint):
    manifest, _, _ = _load(service, fingerprint)
    with mutation_scope(service, manifest), _index_owner(manifest) as index:
        manifest, _, applied = _load(service, fingerprint)
        _require(applied is not None, 'not_applied')
        return _reconcile_owned(service, fingerprint, manifest, index, applied)
