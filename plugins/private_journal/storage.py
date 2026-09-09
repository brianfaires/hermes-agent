"""POSIX descriptor-relative private I/O; no following links or overwrites."""
import os
import secrets
import re
import stat
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def directory(path, create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute safe path required')
    fd = os.open('/', getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    sync_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                    try:
                        os.fsync(sync_fd)
                    finally:
                        os.close(sync_fd)
                except FileExistsError:
                    pass
            try:
                st = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise
            if not stat.S_ISDIR(st.st_mode):
                raise NotADirectoryError('unsafe directory component')
            nxt = os.open(part, getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            nxt_st = os.fstat(nxt)
            if not stat.S_ISDIR(nxt_st.st_mode):
                os.close(nxt)
                raise NotADirectoryError('unsafe directory component')
            os.close(fd)
            fd = nxt
        readable = os.open('.', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = readable
        if create:
            os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def read(path, *, private=True):
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if stat.S_ISREG(st.st_mode) and st.st_nlink == 2:
                # A crash can leave the publication's temporary hard link.
                # Recover only when BOTH links are proven inside this private
                # directory. An arbitrary external hard link is still rejected.
                for name in os.listdir(parent):
                    if not re.fullmatch(r"\.[0-9a-f]{32}\.tmp", name):
                        continue
                    sibling = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (sibling.st_dev, sibling.st_ino) == (st.st_dev, st.st_ino):
                        os.unlink(name, dir_fd=parent)
                        os.fsync(parent)
                        break
                st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or (private and st.st_mode & 0o077):
                raise ValueError('unsafe private file')
            return stream.read()


def publish(path, data, verify=True):
    path = Path(path)
    with directory(path.parent, create=True) as parent:
        tmp = '.' + secrets.token_hex(16) + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(tmp, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            except FileExistsError:
                if not verify:
                    raise
                if read(path) != data:
                    raise ValueError('immutable publication conflict')
            finally:
                try:
                    os.unlink(tmp, dir_fd=parent)
                except FileNotFoundError:
                    pass  # a concurrent reader recovered the publication link
            os.fsync(parent)
        finally:
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass


def remove_verified(path, expected):
    path = Path(path)
    with directory(path.parent) as parent:
        # The containing private directory is owner-only. No other UID may
        # exchange this name between verification and unlink.
        if read(path) != expected:
            raise ValueError('archive mismatch')
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
