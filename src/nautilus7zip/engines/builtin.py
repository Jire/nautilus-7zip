# SPDX-License-Identifier: MIT
"""Built-in engines that only need the Python standard library.

They make ZIP, TAR (plain, gzip, bzip2, xz, lzma and — with Python 3.14 —
zstd) and single-file .gz/.bz2/.xz/.lzma/.zst archives work out of the box
when 7-Zip is not installed.  Compressed tarballs always use the TAR engine
(even when 7-Zip is present) so they extract in one step instead of 7-Zip's
two (".tar.gz" → ".tar" → files).

Extraction never trusts member names: ".." components are rejected, absolute
paths are made relative and nothing is ever written through a symbolic link
that points outside the destination.
"""

from __future__ import annotations

import io
import os
import stat
import struct
import subprocess
import threading
import time
import zipfile

from .. import formats
from ..i18n import _
from ..tool import ToolInfo, tool_environment
from .base import (
    ArchiveError,
    ArchiveInfo,
    Callbacks,
    Cancelled,
    Entry,
    ErrorKind,
    PasswordBox,
    Report,
    UnsafePath,
    clean_member_path,
    describe_os_error,
    ensure_directory,
    is_within,
    safe_target,
)

CHUNK = 1024 * 1024

ZIP_METHODS = {
    0: "Store", 1: "Shrink", 6: "Implode", 8: "Deflate", 9: "Deflate64", 12: "BZip2", 14: "LZMA",
    93: "Zstandard", 95: "XZ", 96: "JPEG", 97: "WavPack", 98: "PPMd", 99: "AES",
}


def _needs_7zip(archive: str, what: str) -> ArchiveError:
    return ArchiveError(
        _("{what} needs 7-Zip, which is not installed.").format(what=what),
        kind=ErrorKind.NEEDS_7ZIP,
        archive=archive,
    )


class _CountingReader(io.RawIOBase):
    """Wraps a file and reports how much of it has been consumed."""

    def __init__(self, raw, total: int, on_progress) -> None:
        super().__init__()
        self._raw = raw
        self.total = max(total, 1)
        self.consumed = 0
        self._on_progress = on_progress
        self._last = 0.0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        n = self._raw.readinto(buffer)
        if n:
            self.consumed += n
            now = time.monotonic()
            if now - self._last > 0.1:
                self._last = now
                self._on_progress(self.consumed / self.total)
        return n

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


class _Progress:
    """Throttled progress reporting for byte-oriented work."""

    def __init__(self, cb: Callbacks, total_bytes: int | None, total_files: int | None) -> None:
        self.cb = cb
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.done_bytes = 0
        self.files = 0
        self.fraction: float | None = None
        self.current: str | None = None
        self._last = 0.0

    def update(self, *, add_bytes: int = 0, fraction: float | None = None, current: str | None = None,
               force: bool = False) -> None:
        self.done_bytes += add_bytes
        if fraction is not None:
            self.fraction = fraction
        if current is not None:
            self.current = current
        now = time.monotonic()
        if not force and now - self._last < 0.08:
            return
        self._last = now
        frac = self.fraction
        if frac is None and self.total_bytes:
            frac = min(self.done_bytes / self.total_bytes, 1.0)
        done = self.done_bytes if self.total_bytes else None
        if frac is not None and self.total_bytes and self.fraction is not None:
            done = int(self.total_bytes * frac)
        self.cb.progress(frac, current=self.current, done_bytes=done, total_bytes=self.total_bytes,
                         files=self.files, total_files=self.total_files)


def _member_selected(path: str, members: list[str] | None) -> bool:
    if not members:
        return True
    for member in members:
        prefix = member.strip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _apply_metadata(path: str, mode: int | None, mtime: float | None, *, is_link: bool = False) -> None:
    if mode is not None and not is_link:
        try:
            os.chmod(path, stat.S_IMODE(mode) & 0o777)
        except OSError:
            pass
    if mtime is not None:
        try:
            os.utime(path, (mtime, mtime), follow_symlinks=not is_link)
        except (OSError, NotImplementedError, ValueError, OverflowError):
            pass


class _Writer:
    """Creates members inside a staging directory, safely."""

    def __init__(self, root: str, cb: Callbacks, progress: _Progress, report: Report, flatten: bool) -> None:
        self.root = root
        self.root_real = os.path.realpath(root)
        self.cb = cb
        self.progress = progress
        self.report = report
        self.flatten = flatten
        self.dirs: list[tuple[str, int | None, float | None]] = []
        self.written: dict[str, str] = {}  # member path → file on disk (for hard links)

    def target_parts(self, name: str, *, backslashes: bool = False) -> list[str] | None:
        parts = clean_member_path(name, backslashes=backslashes)
        if parts is None:
            self.report.warnings.append(_("Skipped “{name}”: unsafe path.").format(name=name))
            return None
        if not parts:
            return None
        if self.flatten:
            parts = parts[-1:]
        return parts

    def _unique(self, path: str) -> str:
        return formats.unique_sibling(path) if os.path.lexists(path) else path

    def make_dir(self, parts: list[str], mode: int | None, mtime: float | None) -> None:
        if self.flatten:
            return
        try:
            path = ensure_directory(self.root, self.root_real, parts)
        except UnsafePath:
            self.report.warnings.append(_("Skipped “{name}”: unsafe path.").format(name="/".join(parts)))
            return
        self.dirs.append((path, mode, mtime))
        self.report.folders += 1

    def write_file(self, parts: list[str], source, size: int | None, mode: int | None, mtime: float | None,
                   member: str) -> None:
        try:
            if self.flatten:
                target = self._unique(os.path.join(self.root, parts[-1]))
            else:
                target = safe_target(self.root, self.root_real, parts)
        except UnsafePath:
            self.report.warnings.append(_("Skipped “{name}”: unsafe path.").format(name="/".join(parts)))
            return
        except IsADirectoryError:
            self.report.warnings.append(_("Skipped “{name}”: a folder has the same name.").format(name=member))
            return
        self.progress.update(current=member)
        try:
            with open(target, "wb") as out:
                while True:
                    self.cb.checkpoint()
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    self.progress.update(add_bytes=len(chunk))
        except BaseException:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
        _apply_metadata(target, mode, mtime)
        self.written[member] = target
        self.report.files += 1
        self.report.size += size or 0
        self.progress.files = self.report.files

    def write_symlink(self, parts: list[str], link_target: str, mtime: float | None, member: str) -> None:
        try:
            if self.flatten:
                target = self._unique(os.path.join(self.root, parts[-1]))
            else:
                target = safe_target(self.root, self.root_real, parts)
            os.symlink(link_target, target)
        except UnsafePath:
            self.report.warnings.append(_("Skipped “{name}”: unsafe path.").format(name=member))
            return
        except IsADirectoryError:
            self.report.warnings.append(_("Skipped “{name}”: a folder has the same name.").format(name=member))
            return
        _apply_metadata(target, None, mtime, is_link=True)
        self.written[member] = target
        self.report.files += 1

    def write_hardlink(self, parts: list[str], link_member: str, mode, mtime, member: str) -> None:
        normalized = link_member
        while normalized.startswith("./"):
            normalized = normalized[2:]
        source = self.written.get(normalized.strip("/")) or self.written.get(link_member)
        if source is None:
            self.report.warnings.append(
                _("Skipped hard link “{name}”: its target was not extracted.").format(name=member))
            return
        try:
            target = self._unique(os.path.join(self.root, parts[-1])) if self.flatten else \
                safe_target(self.root, self.root_real, parts)
            if not is_within(self.root_real, os.path.realpath(source)):
                raise UnsafePath(source)
            os.link(source, target)
        except UnsafePath:
            self.report.warnings.append(_("Skipped “{name}”: unsafe path.").format(name=member))
            return
        except OSError:
            import shutil

            shutil.copy2(source, target)
        self.written[member] = target
        self.report.files += 1

    def finish(self) -> None:
        # Deepest folders first so setting a parent's time sticks.
        for path, mode, mtime in sorted(self.dirs, key=lambda d: d[0].count(os.sep), reverse=True):
            safe_mode = None if mode is None else (stat.S_IMODE(mode) & 0o777) | 0o700  # never lock ourselves out
            _apply_metadata(path, safe_mode, mtime)


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------


def _zip_entry_name(info: zipfile.ZipInfo) -> str:
    """Best-effort Unicode name: UTF-8 flag, Info-ZIP Unicode extra field,
    raw UTF-8 (common on Linux/macOS) and finally CP437 like the spec says."""
    name = info.filename
    if not info.flag_bits & 0x800:
        unicode_name = _zip_unicode_path(info)
        if unicode_name is not None:
            name = unicode_name
        else:
            try:
                raw = name.encode("cp437")
            except UnicodeEncodeError:
                raw = None
            if raw is not None and any(b > 0x7F for b in raw):
                try:
                    name = raw.decode("utf-8")
                except UnicodeDecodeError:
                    pass
    if info.create_system in (0, 11, 14) and "\\" in name:
        name = name.replace("\\", "/")
    return name


def _zip_extra_fields(extra: bytes):
    i = 0
    while i + 4 <= len(extra):
        header_id, size = struct.unpack_from("<HH", extra, i)
        yield header_id, extra[i + 4:i + 4 + size]
        i += 4 + size


def _zip_unicode_path(info: zipfile.ZipInfo) -> str | None:
    for header_id, data in _zip_extra_fields(info.extra):
        if header_id == 0x7075 and len(data) >= 5 and data[0] == 1:
            try:
                return data[5:].decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


def _zip_mtime(info: zipfile.ZipInfo) -> float | None:
    for header_id, data in _zip_extra_fields(info.extra):
        if header_id == 0x5455 and len(data) >= 5 and data[0] & 1:  # extended timestamp, UTC
            return float(struct.unpack_from("<i", data, 1)[0])
    try:
        return time.mktime((*info.date_time, 0, 0, -1))
    except (OverflowError, ValueError):
        return None


def _zip_method(info: zipfile.ZipInfo) -> str:
    if info.compress_type == 99:
        for header_id, data in _zip_extra_fields(info.extra):
            if header_id == 0x9901 and len(data) >= 7:
                strength = {1: "128", 2: "192", 3: "256"}.get(data[4], "")
                inner = ZIP_METHODS.get(struct.unpack_from("<H", data, 5)[0], "?")
                return f"AES-{strength} {inner}".strip()
        return "AES"
    return ZIP_METHODS.get(info.compress_type, str(info.compress_type))


def _zip_mode(info: zipfile.ZipInfo) -> int | None:
    if info.create_system == 3:
        mode = info.external_attr >> 16
        return mode or None
    return None


class ZipEngine:
    name = _("Built-in ZIP support")

    def __init__(self, tool: ToolInfo | None = None) -> None:
        self.tool = tool

    def _open(self, archive: str) -> zipfile.ZipFile:
        base = archive[:-4] if archive.lower().endswith(".zip") else None
        if base and os.path.exists(base + ".z01"):
            raise _needs_7zip(archive, _("Opening split ZIP archives"))
        try:
            return zipfile.ZipFile(archive)
        except zipfile.BadZipFile as exc:
            raise ArchiveError(
                _("“{name}” is not a ZIP archive, or it is damaged.").format(name=os.path.basename(archive)),
                kind=ErrorKind.NOT_ARCHIVE, details=str(exc), archive=archive) from exc
        except OSError as exc:
            message, kind = describe_os_error(exc, archive)
            raise ArchiveError(message, kind=kind, archive=archive) from exc

    def _entries(self, zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Entry]]:
        out = []
        for info in zf.infolist():
            name = _zip_entry_name(info)
            mode = _zip_mode(info)
            is_dir = name.endswith("/") or (bool(info.external_attr & 0x10) and info.file_size == 0)
            is_link = mode is not None and stat.S_ISLNK(mode)
            entry = Entry(
                path=name.strip("/"),
                is_dir=is_dir,
                size=None if is_dir else info.file_size,
                packed_size=None if is_dir else info.compress_size,
                mtime=_zip_mtime(info),
                encrypted=bool(info.flag_bits & 0x1),
                crc=None if is_dir else f"{info.CRC:08X}",
                method=None if is_dir else _zip_method(info),
                link_target="" if is_link else None,
                mode=mode,
                comment=info.comment.decode("utf-8", "replace") if info.comment else None,
            )
            out.append((info, entry))
        return out

    def list(self, archive: str, cb: Callbacks, password: PasswordBox) -> ArchiveInfo:
        with self._open(archive) as zf:
            info = ArchiveInfo(path=archive, type="zip", engine=self.name)
            info.physical_size = os.path.getsize(archive)
            pairs = self._entries(zf)
            info.entries = [entry for _zi, entry in pairs]
            if zf.comment:
                info.comment = zf.comment.decode("utf-8", "replace")
            for zi, entry in pairs:
                if entry.link_target is not None and zi.file_size < 4096 and not entry.encrypted:
                    try:
                        entry.link_target = zf.read(zi).decode("utf-8", "replace")
                    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, OSError):
                        pass
            methods = sorted({e.method for e in info.entries if e.method})
            info.method = " ".join(methods) or None
            return info

    def _open_member(self, zf: zipfile.ZipFile, zi: zipfile.ZipInfo, cb: Callbacks, password: PasswordBox,
                     archive: str):
        if zi.compress_type == 99 or zi.flag_bits & 0x40:
            raise _needs_7zip(archive, _("Opening AES-encrypted ZIP archives"))
        try:
            if not zi.flag_bits & 0x1:
                return zf.open(zi)
            retry = False
            while True:
                pw = password.obtain(cb, os.path.basename(archive), retry=retry)
                try:
                    handle = zf.open(zi, pwd=pw.encode("utf-8"))
                    password.validated = True
                    return handle
                except RuntimeError as exc:
                    if "password" in str(exc).lower():
                        retry = True
                        continue
                    raise
        except NotImplementedError as exc:
            method = ZIP_METHODS.get(zi.compress_type, str(zi.compress_type))
            raise _needs_7zip(archive, _("The “{method}” compression method").format(method=method)) from exc

    def verify_password(self, archive: str, info: ArchiveInfo, cb: Callbacks, password: PasswordBox) -> None:
        if password.validated or not info.encrypted:
            return
        with self._open(archive) as zf:
            for zi, entry in self._entries(zf):
                if entry.encrypted and not entry.is_dir:
                    handle = self._open_member(zf, zi, cb, password, archive)
                    handle.close()
                    return

    def _run(self, archive: str, cb: Callbacks, password: PasswordBox, *, staging: str | None,
             members: list[str] | None, flatten: bool, total_bytes: int | None,
             total_files: int | None) -> Report:
        report = Report()
        progress = _Progress(cb, total_bytes, total_files)
        writer = _Writer(staging, cb, progress, report, flatten) if staging else None
        with self._open(archive) as zf:
            for zi, entry in self._entries(zf):
                cb.checkpoint()
                if not _member_selected(entry.path, members):
                    continue
                if entry.is_dir:
                    if writer:
                        parts = writer.target_parts(entry.path)
                        if parts:
                            writer.make_dir(parts, entry.mode, entry.mtime)
                    else:
                        report.folders += 1
                    continue
                parts = writer.target_parts(entry.path) if writer else [entry.path]
                if not parts:
                    continue
                handle = self._open_member(zf, zi, cb, password, archive)
                try:
                    with handle:
                        if writer is None:  # testing
                            progress.update(current=entry.path)
                            while True:
                                cb.checkpoint()
                                chunk = handle.read(CHUNK)
                                if not chunk:
                                    break
                                progress.update(add_bytes=len(chunk))
                            report.files += 1
                            report.size += zi.file_size
                            progress.files = report.files
                        elif entry.link_target is not None:
                            target = handle.read(65536).decode("utf-8", "surrogateescape")
                            writer.write_symlink(parts, target, entry.mtime, entry.path)
                        else:
                            writer.write_file(parts, handle, zi.file_size, entry.mode, entry.mtime, entry.path)
                except zipfile.BadZipFile as exc:
                    message = str(exc)
                    if zi.flag_bits & 0x1 and "crc" in message.lower():
                        report.errors.append(_("Wrong password for “{name}”.").format(name=entry.path))
                        report.error_kind = ErrorKind.WRONG_PASSWORD
                    else:
                        report.errors.append(_("Data error in “{name}”: the archive is damaged.").format(
                            name=entry.path))
                        report.error_kind = report.error_kind or ErrorKind.DAMAGED
                except (EOFError, zipfile.LargeZipFile) as exc:
                    report.errors.append(f"{entry.path}: {exc}")
                    report.error_kind = report.error_kind or ErrorKind.TRUNCATED
                except OSError as exc:
                    message, kind = describe_os_error(exc)
                    raise ArchiveError(message, kind=kind, archive=archive) from exc
        if writer:
            writer.finish()
        report.packed = os.path.getsize(archive)
        progress.update(fraction=1.0, force=True)
        return report

    def extract(self, archive: str, staging: str, cb: Callbacks, password: PasswordBox, *,
                members: list[str] | None = None, flatten: bool = False, total_bytes: int | None = None,
                total_files: int | None = None) -> Report:
        return self._run(archive, cb, password, staging=staging, members=members, flatten=flatten,
                         total_bytes=total_bytes, total_files=total_files)

    def test(self, archive: str, cb: Callbacks, password: PasswordBox, *, total_bytes: int | None = None,
             total_files: int | None = None) -> Report:
        return self._run(archive, cb, password, staging=None, members=None, flatten=False,
                         total_bytes=total_bytes, total_files=total_files)


# ---------------------------------------------------------------------------
# TAR and single-file streams
# ---------------------------------------------------------------------------


def _python_decompressor(compression: str, fileobj):
    if compression == "gz":
        import gzip

        return gzip.GzipFile(fileobj=fileobj, mode="rb")
    if compression == "bz2":
        import bz2

        return bz2.BZ2File(fileobj, mode="rb")
    if compression in ("xz", "lzma"):
        import lzma

        return lzma.LZMAFile(fileobj, mode="rb", format=lzma.FORMAT_AUTO)
    if compression == "zst":
        from compression import zstd  # novermin - Python 3.14+

        return zstd.ZstdFile(fileobj, mode="rb")
    return None


_MAGIC = (
    (b"\x1f\x8b", "gz"),
    (b"BZh", "bz2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zst"),
    (b"\x1f\x9d", "Z"),
    (b"\x5d\x00\x00", "lzma"),
)


def sniff_compression(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    return None


class _ToolPipe:
    """Decompresses with ``7z x -so`` for filters Python lacks (.Z, old zstd)."""

    def __init__(self, tool: ToolInfo, archive: str, cb: Callbacks, on_fraction) -> None:
        from .sevenzip import _Terminal  # local import: avoids a cycle

        self.proc = subprocess.Popen(
            [tool.path, "x", "-so", "-sccUTF-8", "-bso0", "-bse2", "-bsp2", "--", archive],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=tool_environment(), start_new_session=True)
        self.cb = cb
        cb.attach_process(self.proc)
        self.errors: list[str] = []
        term = _Terminal(lambda line: self.errors.append(line) if line.strip() else None,
                         lambda pct, _count, _name: on_fraction(pct / 100.0))

        def pump() -> None:
            import codecs

            dec = codecs.getincrementaldecoder("utf-8")("replace")
            for data in iter(lambda: self.proc.stderr.read1(4096), b""):
                term.feed(dec.decode(data))

        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()
        self.stream = self.proc.stdout

    def close(self) -> None:
        from .sevenzip import _stop

        try:
            self.stream.close()
        except OSError:
            pass
        _stop(self.proc)
        self.thread.join(timeout=2)
        self.cb.detach_process(self.proc)


class TarEngine:
    """TAR archives (optionally compressed) and single-file compressed streams."""

    name = _("Built-in TAR support")

    def __init__(self, fmt: formats.Format, tool: ToolInfo | None = None) -> None:
        self.fmt = fmt
        self.tool = tool

    # -- opening ----------------------------------------------------------
    def _open_stream(self, archive: str, cb: Callbacks, progress: _Progress):
        compression = sniff_compression(archive)
        if compression is None and self.fmt.compression and self.fmt.builtin == formats.STREAM:
            compression = self.fmt.compression
        try:
            size = os.path.getsize(archive)
            raw = open(archive, "rb")  # noqa: SIM115 - closed via the returned closer
        except OSError as exc:
            message, kind = describe_os_error(exc, archive)
            raise ArchiveError(message, kind=kind, archive=archive) from exc

        def on_fraction(fraction: float) -> None:
            progress.update(fraction=min(fraction, 1.0))

        if compression is None:
            reader = io.BufferedReader(_CountingReader(raw, size, on_fraction), CHUNK)
            return reader, reader.close
        if compression in formats.python_compressions():
            counting = io.BufferedReader(_CountingReader(raw, size, on_fraction), CHUNK)
            stream = _python_decompressor(compression, counting)

            def closer() -> None:
                stream.close()
                counting.close()

            return stream, closer
        raw.close()
        if self.tool is not None:
            pipe = _ToolPipe(self.tool, archive, cb, on_fraction)
            return pipe.stream, pipe.close
        label = {"zst": "Zstandard", "Z": _("Unix compress (.Z)"), "lzma": "LZMA"}.get(compression, compression)
        raise _needs_7zip(archive, _("Decompressing {format} data").format(format=label))

    def _is_tar(self) -> bool:
        return self.fmt.builtin == formats.TAR

    def _stream_member_name(self, archive: str) -> str:
        name = os.path.basename(archive)
        stem, fmt = formats.split_extension(name)
        if fmt is not None and fmt.builtin == formats.STREAM:
            return stem or "data"
        return os.path.splitext(name)[0] or "data"

    # -- listing -------------------------------------------------------------
    def list(self, archive: str, cb: Callbacks, password: PasswordBox) -> ArchiveInfo:
        import tarfile

        cb.stage(_("Reading archive…"))
        progress = _Progress(cb, None, None)
        info = ArchiveInfo(path=archive, type=self.fmt.label.lower(), engine=self.name)
        info.physical_size = os.path.getsize(archive)
        stream, closer = self._open_stream(archive, cb, progress)
        try:
            if not self._is_tar():
                size = 0
                while True:
                    cb.checkpoint()
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                st = os.stat(archive)
                info.entries = [Entry(self._stream_member_name(archive), size=size, packed_size=st.st_size,
                                      mtime=st.st_mtime, method=self.fmt.label)]
                return info
            with tarfile.open(fileobj=stream, mode="r|") as tf:
                for member in tf:
                    cb.checkpoint()
                    entry = _tar_entry(member)
                    if entry is not None:
                        info.entries.append(entry)
        except (tarfile.TarError, EOFError, OSError, ValueError, *_decompression_errors()) as exc:
            if isinstance(exc, OSError) and getattr(exc, "errno", None):
                message, kind = describe_os_error(exc)
                raise ArchiveError(message, kind=kind, archive=archive) from exc
            raise _stream_error(archive, exc) from exc
        finally:
            closer()
        if self.fmt.compression:
            info.method = self.fmt.compression
        return info

    def verify_password(self, archive, info, cb, password) -> None:
        return

    # -- extraction / testing -----------------------------------------------
    def _run(self, archive: str, cb: Callbacks, *, staging: str | None, members: list[str] | None,
             flatten: bool, total_bytes: int | None, total_files: int | None) -> Report:
        import tarfile

        report = Report()
        progress = _Progress(cb, total_bytes, total_files)
        writer = _Writer(staging, cb, progress, report, flatten) if staging else None
        stream, closer = self._open_stream(archive, cb, progress)
        try:
            if not self._is_tar():
                name = self._stream_member_name(archive)
                st = os.stat(archive)
                if writer is not None:
                    writer.write_file([name], stream, None, None, st.st_mtime, name)
                else:
                    while True:
                        cb.checkpoint()
                        chunk = stream.read(CHUNK)
                        if not chunk:
                            break
                        progress.update(add_bytes=len(chunk))
                    report.files = 1
                report.size = progress.done_bytes
            else:
                with tarfile.open(fileobj=stream, mode="r|") as tf:
                    for member in tf:
                        cb.checkpoint()
                        entry = _tar_entry(member)
                        if entry is None or not _member_selected(entry.path, members):
                            continue
                        progress.update(current=entry.path)
                        if writer is None:
                            if member.isreg():
                                source = tf.extractfile(member)
                                while True:
                                    cb.checkpoint()
                                    chunk = source.read(CHUNK)
                                    if not chunk:
                                        break
                                    progress.update(add_bytes=len(chunk))
                                report.files += 1
                                report.size += member.size
                                progress.files = report.files
                            elif member.isdir():
                                report.folders += 1
                            continue
                        parts = writer.target_parts(entry.path)
                        if not parts:
                            continue
                        if member.isdir():
                            writer.make_dir(parts, member.mode, member.mtime)
                        elif member.isreg():
                            writer.write_file(parts, tf.extractfile(member), member.size, member.mode,
                                              member.mtime, entry.path)
                        elif member.issym():
                            writer.write_symlink(parts, member.linkname, member.mtime, entry.path)
                        elif member.islnk():
                            writer.write_hardlink(parts, member.linkname, member.mode, member.mtime, entry.path)
                        else:
                            report.warnings.append(
                                _("Skipped special file “{name}”.").format(name=entry.path))
        except Cancelled:
            raise
        except ArchiveError:
            raise
        except (tarfile.TarError, EOFError, ValueError, OSError, *_decompression_errors()) as exc:
            if isinstance(exc, OSError) and getattr(exc, "errno", None):
                message, kind = describe_os_error(exc)
                raise ArchiveError(message, kind=kind, archive=archive) from exc
            error = _stream_error(archive, exc)
            report.errors.append(error.message)
            report.error_kind = error.kind
            report.warnings.append(str(exc))
        finally:
            closer()
        if writer is not None:
            writer.finish()
        report.packed = os.path.getsize(archive)
        progress.update(fraction=1.0, force=True)
        return report

    def extract(self, archive: str, staging: str, cb: Callbacks, password: PasswordBox, *,
                members: list[str] | None = None, flatten: bool = False, total_bytes: int | None = None,
                total_files: int | None = None) -> Report:
        return self._run(archive, cb, staging=staging, members=members, flatten=flatten,
                         total_bytes=total_bytes, total_files=total_files)

    def test(self, archive: str, cb: Callbacks, password: PasswordBox, *, total_bytes: int | None = None,
             total_files: int | None = None) -> Report:
        return self._run(archive, cb, staging=None, members=None, flatten=False, total_bytes=total_bytes,
                         total_files=total_files)


def _decompression_errors() -> tuple[type[BaseException], ...]:
    import lzma
    import zlib

    errors: list[type[BaseException]] = [zlib.error, lzma.LZMAError]
    try:
        from compression import zstd  # novermin - Python 3.14+

        errors.append(zstd.ZstdError)
    except ImportError:
        pass
    return tuple(errors)


def _tar_entry(member) -> Entry | None:
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    name = name.strip("/")
    if not name or name == ".":
        return None
    return Entry(
        path=name,
        is_dir=member.isdir(),
        size=None if member.isdir() else member.size,
        mtime=float(member.mtime) if member.mtime else None,
        link_target=member.linkname if member.issym() else None,
        mode=member.mode,
    )


def _stream_error(archive: str, exc: BaseException) -> ArchiveError:
    name = os.path.basename(archive)
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, EOFError) or "unexpected end" in lower or "truncated" in lower or "end of file" in lower:
        return ArchiveError(_("The archive is incomplete. It may not have finished downloading."),
                            kind=ErrorKind.TRUNCATED, details=text, archive=archive)
    if "not a gzip file" in lower or "invalid data stream" in lower or "invalid header" in lower or \
            "file could not be opened successfully" in lower or "unknown frame descriptor" in lower:
        return ArchiveError(_("“{name}” is not a supported archive, or it is damaged.").format(name=name),
                            kind=ErrorKind.NOT_ARCHIVE, details=text, archive=archive)
    return ArchiveError(_("The archive is damaged."), kind=ErrorKind.DAMAGED, details=text, archive=archive)
