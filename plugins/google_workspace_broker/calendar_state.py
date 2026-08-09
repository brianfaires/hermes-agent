from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .errors import CalendarStateError


class CalendarOwnershipState:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _check_path(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError as exc:
            raise CalendarStateError("calendar ownership state missing") from exc
        if stat.S_ISLNK(st.st_mode):
            raise CalendarStateError("calendar ownership state must not be a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise CalendarStateError("calendar ownership state must be a regular file")
        if st.st_uid != os.geteuid():
            raise CalendarStateError("calendar ownership state must be owned by broker uid")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise CalendarStateError("calendar ownership state must have mode 0600")
        parent_st = os.stat(self.path.parent)
        if parent_st.st_uid != os.geteuid():
            raise CalendarStateError("calendar ownership state parent must be owned by broker uid")
        if parent_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CalendarStateError("calendar ownership state parent is insecure")
        if not os.access(self.path, os.R_OK | os.W_OK):
            raise CalendarStateError("calendar ownership state is not readable and writable")

    def preflight_writable(self) -> None:
        self._load()

    def _load(self) -> set[str]:
        self._check_path()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CalendarStateError("calendar ownership state is malformed") from exc
        ids = payload.get("calendar_ids") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not all(isinstance(v, str) and v for v in ids):
            raise CalendarStateError("calendar ownership state is malformed")
        return set(ids)

    def contains(self, calendar_id: str) -> bool:
        return calendar_id in self._load()

    def add(self, calendar_id: str) -> None:
        ids = self._load()
        ids.add(calendar_id)
        payload = {"calendar_ids": sorted(ids)}
        fd, tmp_name = tempfile.mkstemp(prefix=".calendar-state-", dir=str(self.path.parent))
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600)
            dir_fd = os.open(self.path.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
