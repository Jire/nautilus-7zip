# SPDX-License-Identifier: MIT
"""Engine selection: 7-Zip when it can do the job, built-in Python otherwise."""

from __future__ import annotations

import os

from .. import formats, tool
from ..i18n import _
from .base import ArchiveError, ErrorKind

_SNIFF = (
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
    (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"),
    (b"\x1f\x8b", "gz"), (b"BZh", "bz2"), (b"\xfd7zXZ\x00", "xz"), (b"\x28\xb5\x2f\xfd", "zst"),
)


def sniff(path: str) -> str | None:
    """Identify the container from its first bytes (cheap, ~1 read)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    for magic, name in _SNIFF:
        if head.startswith(magic):
            return name
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return None


def effective_format(path: str, mime: str | None = None) -> formats.Format | None:
    """The catalog format, corrected by content sniffing where it matters.

    Examples: Alpine's ".apk" packages are tarballs rather than ZIPs, and a
    download named ".zip" that really is a 7z archive still opens.
    """
    fmt = formats.detect(os.path.basename(path), mime)
    magic = sniff(path)
    if fmt is not None and fmt.builtin == formats.ZIP and magic not in (None, "zip"):
        return formats.by_id(_MAGIC_FORMAT.get(magic, fmt.id))
    if fmt is None and magic is not None:
        return formats.by_id(_MAGIC_FORMAT[magic])
    return fmt


_MAGIC_FORMAT = {"zip": "zip", "7z": "7z", "rar": "rar", "tar": "tar", "gz": "tar.gz", "bz2": "bz2", "xz": "xz",
                 "zst": "zst"}


def engine_for(path: str, settings=None):
    """Return an engine able to read ``path`` or raise ArchiveError."""
    from .builtin import TarEngine, ZipEngine
    from .sevenzip import SevenZipEngine

    override = settings.get("sevenzip_path") if settings is not None else None
    fmt = effective_format(path)
    best = tool.best_tool(override=override)

    # Compressed tarballs extract in one step here (7-Zip itself would stop at
    # the inner ".tar"); 7-Zip is only used to decompress filters Python lacks.
    if (fmt is not None and fmt.builtin in (formats.TAR, formats.STREAM) and fmt.compression is not None
            and (not fmt.needs_7zip or best is not None)):
        return TarEngine(fmt, best)
    if best is not None:
        if fmt is not None and fmt.id in ("rar", "cbr"):
            rar_tool = tool.best_tool(read="rar", override=override)
            if rar_tool is not None and rar_tool.has_rar:
                return SevenZipEngine(rar_tool)
        return SevenZipEngine(best)
    if fmt is None:
        raise ArchiveError(
            _("“{name}” is not a recognised archive.").format(name=os.path.basename(path)),
            kind=ErrorKind.NOT_ARCHIVE, archive=path)
    if fmt.builtin == formats.ZIP:
        return ZipEngine()
    if fmt.builtin in (formats.TAR, formats.STREAM) and not fmt.needs_7zip:
        return TarEngine(fmt)
    raise ArchiveError(
        _("Opening {format} archives needs 7-Zip, which is not installed.").format(format=fmt.label),
        kind=ErrorKind.NEEDS_7ZIP, archive=path)
