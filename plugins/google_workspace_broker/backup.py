from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_source(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("backup source could not be opened securely") from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise ValueError("backup source must be a regular file")
    return fd


def create_backup(source_paths: list[str | Path], dest_dir: str | Path, *, approved: bool) -> Path:
    if not approved:
        raise PermissionError("pre-migration backup gate was not approved")
    dest = Path(dest_dir)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError("backup destination already exists")
    try:
        dest.mkdir(mode=0o700, parents=False)
        os.chmod(dest, 0o700)
        files_dir = dest / "files"
        files_dir.mkdir(mode=0o700)
        os.chmod(files_dir, 0o700)
        sources = []
        seen_names: set[str] = set()
        for idx, raw in enumerate(source_paths):
            path = Path(raw)
            src_fd = _open_source(path)
            st = os.fstat(src_fd)
            backup_name = hashlib.sha256(f"{idx}:{path}".encode("utf-8")).hexdigest()
            if backup_name in seen_names:
                os.close(src_fd)
                raise ValueError("backup source collision")
            seen_names.add(backup_name)
            copy_path = files_dir / backup_name
            try:
                fd = os.open(copy_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except Exception:
                os.close(src_fd)
                raise
            try:
                h = hashlib.sha256()
                copied_size = 0
                with os.fdopen(fd, "wb") as out, os.fdopen(src_fd, "rb") as src:
                    src_fd = -1
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        h.update(chunk)
                        copied_size += len(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                source_sha = h.hexdigest()
            except Exception:
                if src_fd >= 0:
                    os.close(src_fd)
                try:
                    copy_path.unlink()
                except FileNotFoundError:
                    pass
                raise
            if copied_size != st.st_size:
                try:
                    copy_path.unlink()
                except FileNotFoundError:
                    pass
                raise ValueError("backup source changed while copying")
            os.chmod(copy_path, 0o600)
            os.utime(copy_path, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
            copy_sha = _sha256(copy_path)
            if copy_sha != source_sha or copy_path.stat().st_size != copied_size:
                try:
                    copy_path.unlink()
                except FileNotFoundError:
                    pass
                raise ValueError("backup copy checksum mismatch")
            sources.append({
                "path": str(path),
                "backup_name": backup_name,
                "mode": oct(stat.S_IMODE(st.st_mode)),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "source_sha256": source_sha,
                "copy_sha256": copy_sha,
            })
        manifest = {
            "format": "google-workspace-broker-backup-manifest-v1",
            "note": "Manifest records metadata and checksums. Secret contents are copied only into restrictive backup files, never printed.",
            "sources": sources,
        }
        manifest_path = dest / "manifest.json"
        fd = os.open(manifest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(manifest_path, 0o600)
        return manifest_path
    except Exception:
        if dest.exists() and not dest.is_symlink():
            shutil.rmtree(dest)
        raise
