"""Exact-byte, single-file apply mechanics; private to the native proposal owner.

The durable intent precedes replacement. Its expected destination inode permits
recovery of write-before-receipt only for the exact replacement this intent owns.
No filesystem effects run inside a SQLite retry callback.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid

from gateway.locked_skill_proposals import _read, _require, _save
from hermes_cli.locked_skill_proposals import regular


def _file_identity(info):
    return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]


@contextmanager
def _directory(path):
    # All operations stay in the already-qualified target directory. Opening '/'
    # for a component walk needlessly requests access outside the owner scope.
    regular(path / 'SKILL.md')
    expected = path.stat()
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        actual = os.fstat(fd)
        _require((expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino), 'target_changed')
        yield fd
    finally:
        os.close(fd)


def _state(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_mode & 0o444, 'unsafe_target')
        return stream.read(), _file_identity(info)


def _load(service, fingerprint):
    with service.db._read_ctx() as conn:
        manifest = service._manifest(conn, fingerprint)
        principal = _read(conn, 'principal')
        receipt = _read(conn, 'approval:' + fingerprint)
        _require(principal is not None and receipt is not None
                 and receipt['manifest_sha256'] == fingerprint
                 and receipt['candidate_digest'] == principal['candidate_digest']
                 and receipt['identity'] == principal['identity'])
        service.check_route(principal['identity'])
        _require(all(manifest[key] == principal['target'][key] for key in
                     ('home', 'owner', 'repository', 'common_dir', 'skill', 'path')), 'target_changed')
        return manifest, _read(conn, 'intent:' + fingerprint), _read(conn, 'application:' + fingerprint)


def _record(service, fingerprint, identity):
    def write(conn):
        intent = _read(conn, 'intent:' + fingerprint)
        _require(intent is not None and intent['post_identity'] == identity, 'target_changed')
        value = {'applied': True, 'manifest_sha256': fingerprint, 'post_identity': identity}
        _save(conn, 'application:' + fingerprint, value)
        return value
    return service.db._execute_write(write)


def _replace(service, fingerprint, manifest, directory):
    preimage = bytes.fromhex(manifest['preimage_hex'])
    postimage = bytes.fromhex(manifest['postimage_hex'])
    name = Path(manifest['path']).name
    _require(_state(directory, name) == (preimage, manifest['file_identity']), 'target_changed')
    temporary = '.locked-proposal-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(postimage)
            stream.flush()
            os.fchmod(stream.fileno(), manifest['file_identity'][2])
            os.fsync(stream.fileno())
            identity = _file_identity(os.fstat(stream.fileno()))
        def intent_write(conn):
            # A replay may have a previous intent after a pre-replacement interruption.
            _save(conn, 'intent:' + fingerprint, {'post_identity': identity})
        service.db._execute_write(intent_write)
        _load(service, fingerprint)
        _require(_state(directory, name) == (preimage, manifest['file_identity']), 'target_changed')
        # The parent handle must still name the same canonical directory immediately before rename.
        parent = Path(manifest['home']) / Path(manifest['path']).parent
        _require(parent.stat().st_ino == os.fstat(directory).st_ino and not parent.is_symlink(), 'target_changed')
        regular(parent / name)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        _require(_state(directory, name) == (postimage, identity), 'readback_failed')
        return _record(service, fingerprint, identity)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def mutation_scope(service, manifest):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.skill_manager_tool import _skill_lock_path, _skill_mutation_lock
    from tools import skill_usage
    _require(os.name == 'posix' and skill_usage.fcntl is not None, 'unsupported_route')
    token = set_hermes_home_override(service.home)
    try:
        # The existing locks are shared with ordinary mutations and usage/lock toggles.
        # New receipt-authorized writes refuse redirected bookkeeping paths as well.
        for path in (_skill_lock_path(manifest['skill']), service.home / 'skills/.usage.json.lock'):
            _require(not path.is_symlink() and not path.parent.is_symlink(), 'unsafe_lock')
            if path.exists():
                regular(path)
        with _skill_mutation_lock(manifest['skill']), skill_usage._usage_file_lock():
            yield
    finally:
        reset_hermes_home_override(token)


def apply(service, fingerprint):
    # Resolve from the owner authority, not a caller-selected cwd/environment profile.
    manifest, _, _ = _load(service, fingerprint)
    with mutation_scope(service, manifest):
        manifest, intent, applied = _load(service, fingerprint)
        target = Path(manifest['home']) / manifest['path']
        regular(target)
        with _directory(target.parent) as directory:
            content, identity = _state(directory, target.name)
            if content == bytes.fromhex(manifest['postimage_hex']):
                _require(intent is not None and intent['post_identity'] == identity, 'target_changed')
                if applied is not None:
                    _require(applied['post_identity'] == identity, 'target_changed')
                    return {**applied, 'already_applied': True}
                return {**_record(service, fingerprint, identity), 'recovered': True}
            _require(applied is None, 'target_changed')
            return _replace(service, fingerprint, manifest, directory)
