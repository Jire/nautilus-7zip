# SPDX-License-Identifier: MIT
"""Common types for the archive engines (7-Zip CLI and built-in Python)."""

from __future__ import annotations

import enum
import os
import stat
from dataclasses import dataclass, field

from ..i18n import _


class Cancelled(Exception):
    """Raised when the user cancels an operation."""


class ErrorKind(enum.Enum):
    GENERIC = "generic"
    WRONG_PASSWORD = "wrong-password"
    NOT_ARCHIVE = "not-archive"
    UNSUPPORTED = "unsupported"
    DAMAGED = "damaged"
    TRUNCATED = "truncated"
    MISSING_VOLUME = "missing-volume"
    DISK_FULL = "disk-full"
    PERMISSION = "permission"
    NEEDS_7ZIP = "needs-7zip"
    NEEDS_RAR = "needs-rar"
    NOT_FOUND = "not-found"
    MEMORY = "memory"


class ArchiveError(Exception):
    """A user-presentable failure."""

    def __init__(self, message: str, *, kind: ErrorKind = ErrorKind.GENERIC, details: str = "",
                 archive: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.details = details
        self.archive = archive


class OverwriteMode(str, enum.Enum):
    ASK = "ask"
    OVERWRITE = "overwrite"
    SKIP = "skip"
    RENAME_NEW = "rename"
    RENAME_EXISTING = "rename-existing"


class Answer(enum.Enum):
    REPLACE = "replace"
    SKIP = "skip"
    KEEP_BOTH = "keep-both"
    CANCEL = "cancel"


ANSWER_TO_MODE = {
    Answer.REPLACE: OverwriteMode.OVERWRITE,
    Answer.SKIP: OverwriteMode.SKIP,
    Answer.KEEP_BOTH: OverwriteMode.RENAME_NEW,
}


@dataclass
class FileDescription:
    """What the overwrite dialog shows about one side of a conflict."""

    path: str
    size: int | None = None
    mtime: float | None = None
    is_dir: bool = False
    is_link: bool = False

    @classmethod
    def from_path(cls, path: str) -> FileDescription:
        try:
            st = os.lstat(path)
        except OSError:
            return cls(path)
        is_dir = stat.S_ISDIR(st.st_mode)
        return cls(path, None if is_dir else st.st_size, st.st_mtime, is_dir, stat.S_ISLNK(st.st_mode))


@dataclass
class Entry:
    """One item inside an archive."""

    path: str  # "/"-separated, relative, no trailing slash
    is_dir: bool = False
    size: int | None = None
    packed_size: int | None = None
    mtime: float | None = None
    encrypted: bool = False
    crc: str | None = None
    method: str | None = None
    link_target: str | None = None
    mode: int | None = None
    block: int | None = None
    comment: str | None = None

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def is_link(self) -> bool:
        return self.link_target is not None


@dataclass
class ArchiveInfo:
    path: str
    type: str = ""
    physical_size: int | None = None
    headers_size: int | None = None
    method: str | None = None
    solid: bool | None = None
    blocks: int | None = None
    headers_encrypted: bool = False
    comment: str | None = None
    volumes: int | None = None
    entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    engine: str = ""

    @property
    def files(self) -> int:
        return sum(1 for e in self.entries if not e.is_dir)

    @property
    def folders(self) -> int:
        return len(self.folder_paths())

    @property
    def total_size(self) -> int:
        return sum(e.size or 0 for e in self.entries if not e.is_dir)

    @property
    def packed_total(self) -> int | None:
        sizes = [e.packed_size for e in self.entries if e.packed_size]
        return sum(sizes) if sizes else None

    @property
    def encrypted(self) -> bool:
        return self.headers_encrypted or any(e.encrypted for e in self.entries)

    def folder_paths(self) -> set[str]:
        """All folders, including the ones only implied by file paths."""
        folders = {e.path for e in self.entries if e.is_dir}
        for e in self.entries:
            parts = e.path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                folders.add("/".join(parts[:i]))
        folders.discard("")
        return folders

    def top_level_names(self) -> set[str]:
        return {e.path.split("/", 1)[0] for e in self.entries if e.path}


@dataclass
class Report:
    """Outcome of an extract/test/create run."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files: int = 0
    folders: int = 0
    size: int = 0
    packed: int | None = None
    error_kind: ErrorKind | None = None
    outputs: list[str] = field(default_factory=list)  # paths created for the user

    @property
    def ok(self) -> bool:
        return not self.errors

    def merge(self, other: Report) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.files += other.files
        self.folders += other.folders
        self.size += other.size
        if other.packed is not None:
            self.packed = (self.packed or 0) + other.packed
        self.error_kind = self.error_kind or other.error_kind
        self.outputs.extend(other.outputs)


class Callbacks:
    """Bridge between an engine running in a worker thread and the UI.

    The base implementation is non-interactive (used by tests and as a
    fallback): it never asks questions, cancels on password prompts and
    skips conflicting files.
    """

    def __init__(self) -> None:
        self.cancelled = False

    def stage(self, text: str) -> None:
        """Describe what is happening ("Reading archive…")."""

    def progress(self, fraction: float | None, *, current: str | None = None, done_bytes: int | None = None,
                 total_bytes: int | None = None, files: int | None = None, total_files: int | None = None,
                 packed: int | None = None) -> None:
        """Report progress; ``fraction`` is in [0, 1] or None if unknown."""

    def ask_password(self, archive_name: str, retry: bool) -> str | None:
        return None

    def ask_overwrite(self, existing: FileDescription, incoming: FileDescription) -> tuple[Answer, bool]:
        return Answer.SKIP, True

    def checkpoint(self) -> None:
        """Raise Cancelled if cancelled; block while paused."""
        if self.cancelled:
            raise Cancelled()

    def attach_process(self, process) -> None:
        """Called with a running subprocess so pause/cancel can signal it."""

    def detach_process(self, process) -> None:
        pass


class PasswordBox:
    """Remembers the password of one archive across list/test/extract runs."""

    def __init__(self, password: str | None = None) -> None:
        self.value = password
        self.validated = False
        self.asked = False

    def obtain(self, cb: Callbacks, archive_name: str, retry: bool = False) -> str:
        if self.value is not None and not retry:
            return self.value
        password = cb.ask_password(archive_name, retry)
        self.asked = True
        if password is None:
            raise Cancelled()
        self.value = password
        self.validated = False
        return password


# ---------------------------------------------------------------------------
# Safe path handling for the built-in engines
# ---------------------------------------------------------------------------


class UnsafePath(Exception):
    pass


def clean_member_path(name: str, *, backslashes: bool = False) -> list[str] | None:
    """Split an archive member name into safe path components.

    Leading slashes, "." components and Windows drive letters are dropped;
    names containing ".." are rejected (returns None).
    """
    if backslashes:
        name = name.replace("\\", "/")
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        name = name[2:]
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if any("\x00" in p for p in parts):
        return None
    return parts


def is_within(root_real: str, path_real: str) -> bool:
    return path_real == root_real or path_real.startswith(root_real.rstrip(os.sep) + os.sep)


def ensure_directory(root: str, root_real: str, parts: list[str]) -> str:
    """Create ``root/parts…`` one component at a time without ever leaving ``root``.

    Every existing component is resolved; a symbolic link that points outside
    the extraction root (planted by a malicious archive) raises UnsafePath.
    """
    current = root
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            real = os.path.realpath(current)
            if not is_within(root_real, real):
                raise UnsafePath(current)
            if not os.path.isdir(real):
                os.unlink(current)
                os.mkdir(current, 0o755)
        elif os.path.isdir(current):
            continue
        elif os.path.lexists(current):
            # A file is in the way of a folder (archives can contain both).
            os.unlink(current)
            os.mkdir(current, 0o755)
        else:
            os.mkdir(current, 0o755)
    return current


def safe_target(root: str, root_real: str, parts: list[str]) -> str:
    """Path for a member file; its parent folder is created safely."""
    parent = ensure_directory(root, root_real, parts[:-1])
    target = os.path.join(parent, parts[-1])
    if os.path.lexists(target):
        if os.path.isdir(target) and not os.path.islink(target):
            raise IsADirectoryError(target)
        os.unlink(target)
    return target


def describe_os_error(exc: OSError, path: str | None = None) -> tuple[str, ErrorKind]:
    import errno

    where = path or getattr(exc, "filename", None) or ""
    name = os.path.basename(where) if where else ""
    if exc.errno == errno.ENOSPC:
        return _("There is not enough space on the disk."), ErrorKind.DISK_FULL
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        folder = os.path.basename(os.path.dirname(where)) or where
        return _("You don’t have permission to write to “{folder}”.").format(folder=folder), ErrorKind.PERMISSION
    if exc.errno == errno.ENOENT:
        return _("“{name}” could not be found.").format(name=name or where), ErrorKind.NOT_FOUND
    if exc.errno == errno.ENAMETOOLONG:
        return _("The name “{name}” is too long for this file system.").format(name=name), ErrorKind.GENERIC
    return f"{exc.strerror or exc}: {where}" if where else str(exc), ErrorKind.GENERIC
