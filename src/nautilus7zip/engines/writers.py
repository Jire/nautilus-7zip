# SPDX-License-Identifier: MIT
"""Built-in archive writers (ZIP, TAR family, single-file streams).

Used whenever 7-Zip is not installed, and for the TAR family in general
because Python's tarfile preserves Unix permissions, symbolic and hard links
exactly like GNU tar does.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile

from ..i18n import _
from ..tool import ToolInfo, tool_environment
from .base import ArchiveError, Callbacks, Cancelled, ErrorKind, Report, describe_os_error

CHUNK = 1024 * 1024


class SourceItem:
    """A file system object to archive, with its name inside the archive."""

    __slots__ = ("arcname", "path", "st")

    def __init__(self, path: str, arcname: str, st: os.stat_result) -> None:
        self.path = path
        self.arcname = arcname
        self.st = st

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.st.st_mode)

    @property
    def is_link(self) -> bool:
        return stat.S_ISLNK(self.st.st_mode)

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.st.st_mode)


def collect(sources: list[str], base: str, cb: Callbacks, *, follow_links: bool = False) -> list[SourceItem]:
    """Walk the sources (depth-first, sorted) relative to ``base``."""
    items: list[SourceItem] = []
    for source in sources:
        path = source.rstrip(os.sep) or source
        _walk(path, os.path.relpath(path, base), items, cb, follow_links)
    return items


def _walk(path: str, arcname: str, items: list[SourceItem], cb: Callbacks, follow: bool) -> None:
    cb.checkpoint()
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except OSError as exc:
        message, kind = describe_os_error(exc, path)
        raise ArchiveError(message, kind=kind) from exc
    items.append(SourceItem(path, arcname.replace(os.sep, "/"), st))
    if stat.S_ISDIR(st.st_mode):
        try:
            names = sorted(os.listdir(path))
        except OSError as exc:
            message, kind = describe_os_error(exc, path)
            raise ArchiveError(message, kind=kind) from exc
        for name in names:
            _walk(os.path.join(path, name), arcname + "/" + name, items, cb, follow)


class _ProgressReader(io.RawIOBase):
    def __init__(self, fh, on_bytes) -> None:
        super().__init__()
        self._fh = fh
        self._on_bytes = on_bytes

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        n = self._fh.readinto(buffer)
        if n:
            self._on_bytes(n)
        return n


class _Meter:
    def __init__(self, cb: Callbacks, total: int, output: str | None) -> None:
        self.cb = cb
        self.total = max(total, 0)
        self.done = 0
        self.files = 0
        self.output = output
        self._last = 0.0
        self.current: str | None = None

    def add(self, n: int, *, force: bool = False) -> None:
        self.done += n
        self.cb.checkpoint()
        now = time.monotonic()
        if not force and now - self._last < 0.08:
            return
        self._last = now
        packed = None
        if self.output:
            try:
                packed = os.stat(self.output).st_size
            except OSError:
                packed = None
        fraction = min(self.done / self.total, 1.0) if self.total else None
        self.cb.progress(fraction, current=self.current, done_bytes=self.done, total_bytes=self.total,
                         files=self.files, packed=packed)


def _zip_info_for(item: SourceItem, compression: int, level: int | None) -> zipfile.ZipInfo:
    name = item.arcname + ("/" if item.is_dir else "")
    mtime = time.localtime(max(item.st.st_mtime, 315532800))  # ZIP cannot store dates before 1980
    info = zipfile.ZipInfo(name, date_time=mtime[:6])
    info.create_system = 3
    info.external_attr = (item.st.st_mode & 0xFFFF) << 16
    if item.is_dir:
        info.external_attr |= 0x10
        info.compress_type = zipfile.ZIP_STORED
    else:
        info.compress_type = compression
        if level is not None and compression != zipfile.ZIP_STORED:
            # Renamed from the private "_compresslevel" in Python 3.13.
            if "compress_level" in getattr(zipfile.ZipInfo, "__slots__", ()):
                info.compress_level = level
            else:
                info._compresslevel = level
        info.file_size = item.st.st_size
    return info


ZIP_COMPRESSION = {
    "store": zipfile.ZIP_STORED,
    "deflate": zipfile.ZIP_DEFLATED,
    "bzip2": zipfile.ZIP_BZIP2,
    "lzma": zipfile.ZIP_LZMA,
}
if hasattr(zipfile, "ZIP_ZSTANDARD"):
    ZIP_COMPRESSION["zstd"] = zipfile.ZIP_ZSTANDARD  # novermin - Python 3.14+


def write_zip(archive: str, items: list[SourceItem], cb: Callbacks, *, level: int, method: str | None,
              store_links: bool, update: bool = True) -> Report:
    """Create (or update, replacing same-named entries) a ZIP archive."""
    report = Report()
    method = (method or ("store" if level == 0 else "deflate")).lower()
    compression = ZIP_COMPRESSION.get(method, zipfile.ZIP_DEFLATED)
    if level == 0:
        compression = zipfile.ZIP_STORED
    zlevel = {1: 1, 3: 3, 5: 6, 7: 8, 9: 9}.get(level, 6) if compression == zipfile.ZIP_DEFLATED else None
    if compression == zipfile.ZIP_BZIP2:
        zlevel = max(1, min(level, 9))
    total = sum(i.st.st_size for i in items if i.is_file)
    directory = os.path.dirname(os.path.abspath(archive)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".7z-tmp-", suffix=".zip", dir=directory)
    os.close(fd)
    meter = _Meter(cb, total, tmp)
    new_names = {i.arcname + ("/" if i.is_dir else "") for i in items}
    try:
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
            if update and os.path.exists(archive):
                _copy_existing(archive, zf, new_names, cb)
            for item in items:
                cb.checkpoint()
                meter.current = item.arcname
                if item.is_link and store_links:
                    info = _zip_info_for(item, zipfile.ZIP_STORED, None)
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    zf.writestr(info, os.readlink(item.path).encode("utf-8", "surrogateescape"))
                elif item.is_dir:
                    zf.writestr(_zip_info_for(item, compression, zlevel), b"")
                    report.folders += 1
                    continue
                elif item.is_file or item.is_link:
                    real = item
                    if item.is_link:
                        try:
                            real = SourceItem(item.path, item.arcname, os.stat(item.path))
                        except OSError:
                            report.warnings.append(_("Skipped broken link “{name}”.").format(name=item.arcname))
                            continue
                    info = _zip_info_for(real, compression, zlevel)
                    with open(item.path, "rb") as src, zf.open(info, "w", force_zip64=real.st.st_size > 2**31) as dst:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            dst.write(chunk)
                            meter.add(len(chunk))
                else:
                    report.warnings.append(_("Skipped special file “{name}”.").format(name=item.arcname))
                    continue
                report.files += 1
                meter.files = report.files
                report.size += item.st.st_size if item.is_file else 0
        os.replace(tmp, archive)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            message, kind = describe_os_error(exc)
            raise ArchiveError(message, kind=kind, archive=archive) from exc
        raise
    meter.add(0, force=True)
    report.packed = os.path.getsize(archive)
    report.outputs.append(archive)
    return report


def _copy_existing(archive: str, out: zipfile.ZipFile, replaced: set[str], cb: Callbacks) -> None:
    try:
        old = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(_("“{name}” already exists and is not a ZIP archive.").format(
            name=os.path.basename(archive)), kind=ErrorKind.NOT_ARCHIVE) from exc
    with old:
        for info in old.infolist():
            cb.checkpoint()
            if info.filename in replaced or info.filename.rstrip("/") + "/" in replaced:
                continue
            if info.flag_bits & 0x1:
                raise ArchiveError(_("Updating encrypted ZIP archives needs 7-Zip, which is not installed."),
                                   kind=ErrorKind.NEEDS_7ZIP)
            with old.open(info) as src, out.open(info, "w", force_zip64=info.file_size > 2**31) as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)


class _CompressorPipe:
    """Feeds a tar stream into ``7z a -si`` for multi-threaded compression."""

    def __init__(self, tool: ToolInfo, archive: str, kind: str, level: int, threads: int | None,
                 cb: Callbacks) -> None:
        args = [tool.path, "a", f"-t{kind}", f"-mx={level}", "-si", "-sccUTF-8", "-bso0", "-bsp0", "-bse2", "-y"]
        if threads:
            args.append(f"-mmt={threads}")
        args += ["--", archive]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, env=tool_environment(), start_new_session=True)
        self.cb = cb
        cb.attach_process(self.proc)
        self._err: list[bytes] = []
        self._thread = threading.Thread(target=lambda: self._err.append(self.proc.stderr.read()), daemon=True)
        self._thread.start()

    def write(self, data) -> int:
        try:
            self.proc.stdin.write(data)
        except BrokenPipeError as exc:
            raise ArchiveError(_("7-Zip stopped unexpectedly."), details=self.errors()) from exc
        return len(data)

    def flush(self) -> None:
        pass

    def errors(self) -> str:
        return b"".join(self._err).decode("utf-8", "replace")

    def finish(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        code = self.proc.wait()
        self._thread.join(timeout=5)
        self.cb.detach_process(self.proc)
        if code == 255:
            raise Cancelled()
        if code not in (0, 1):
            raise ArchiveError(_("7-Zip could not compress the data."), details=self.errors())

    def abort(self) -> None:
        from .sevenzip import _stop

        _stop(self.proc)
        self.cb.detach_process(self.proc)


def _open_compressed_output(path: str, compression: str | None, level: int, threads: int | None,
                            tool: ToolInfo | None, cb: Callbacks):
    """Return (fileobj, finish, abort) writing compressed data to ``path``."""
    if compression in ("xz", "bz2") and tool is not None and tool.can_write("xz" if compression == "xz" else "bzip2"):
        pipe = _CompressorPipe(tool, path, "xz" if compression == "xz" else "bzip2", level, threads, cb)
        return pipe, pipe.finish, pipe.abort
    raw = open(path, "wb")  # noqa: SIM115 - closed by finish/abort

    def closer(stream):
        def finish():
            stream.close()
            if stream is not raw:
                raw.close()

        def abort():
            try:
                stream.close()
            finally:
                raw.close()

        return finish, abort

    if compression is None:
        f, a = closer(raw)
        return raw, f, a
    if compression == "gz":
        import gzip

        stream = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=max(1, min(level, 9)) if level else 1,
                               mtime=0)
    elif compression == "bz2":
        import bz2

        stream = bz2.BZ2File(raw, mode="wb", compresslevel=max(1, min(level, 9)))
    elif compression == "xz":
        import lzma

        preset = min(level, 9) | (lzma.PRESET_EXTREME if level >= 9 else 0)
        stream = lzma.LZMAFile(raw, mode="wb", preset=preset)  # noqa: SIM115 - closed by finish()
    elif compression == "zst":
        try:
            from compression import zstd  # novermin - Python 3.14+
        except ImportError as exc:
            raw.close()
            raise ArchiveError(_("Zstandard compression needs Python 3.14 or newer."),
                               kind=ErrorKind.UNSUPPORTED) from exc
        zlevel = {0: 1, 1: 1, 3: 3, 5: 6, 7: 12, 9: 19}.get(level, 3)
        options = {zstd.CompressionParameter.compression_level: zlevel}
        workers = threads if threads is not None else (os.cpu_count() or 1)
        if workers and workers > 1:
            options[zstd.CompressionParameter.nb_workers] = workers
        stream = zstd.ZstdFile(raw, mode="wb", options=options)
    else:
        raw.close()
        raise ArchiveError(_("Unsupported compression."), kind=ErrorKind.UNSUPPORTED)
    f, a = closer(stream)
    return stream, f, a


def write_tar(archive: str, items: list[SourceItem], cb: Callbacks, *, compression: str | None, level: int,
              threads: int | None, tool: ToolInfo | None, store_links: bool) -> Report:
    report = Report()
    total = sum(i.st.st_size for i in items if i.is_file or (i.is_link and not store_links))
    directory = os.path.dirname(os.path.abspath(archive)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".7z-tmp-", dir=directory)
    os.close(fd)
    os.unlink(tmp)  # the compressor creates it
    meter = _Meter(cb, total, tmp)
    out, finish, abort = _open_compressed_output(tmp, compression, level, threads, tool, cb)
    try:
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT, dereference=not store_links) as tf:
            for item in items:
                cb.checkpoint()
                meter.current = item.arcname
                try:
                    info = tf.gettarinfo(item.path, arcname=item.arcname)
                except OSError as exc:
                    report.warnings.append(f"{item.arcname}: {exc.strerror}")
                    continue
                if info is None:
                    report.warnings.append(_("Skipped special file “{name}”.").format(name=item.arcname))
                    continue
                if info.isreg():
                    with open(item.path, "rb") as fh:
                        tf.addfile(info, io.BufferedReader(_ProgressReader(fh, meter.add), CHUNK))
                    report.files += 1
                    report.size += info.size
                else:
                    tf.addfile(info)
                    if info.isdir():
                        report.folders += 1
                    else:
                        report.files += 1
                meter.files = report.files
        finish()
        os.replace(tmp, archive)
    except BaseException as exc:
        try:
            abort()
        except Exception:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError) and not isinstance(exc, ArchiveError):
            message, kind = describe_os_error(exc)
            raise ArchiveError(message, kind=kind, archive=archive) from exc
        raise
    meter.add(0, force=True)
    report.packed = os.path.getsize(archive)
    report.outputs.append(archive)
    return report


def write_stream(archive: str, item: SourceItem, cb: Callbacks, *, compression: str, level: int,
                 threads: int | None, tool: ToolInfo | None) -> Report:
    """Compress a single file to .gz/.bz2/.xz/.zst."""
    report = Report()
    directory = os.path.dirname(os.path.abspath(archive)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".7z-tmp-", dir=directory)
    os.close(fd)
    os.unlink(tmp)
    meter = _Meter(cb, item.st.st_size, tmp)
    meter.current = item.arcname
    out, finish, abort = _open_compressed_output(tmp, compression, level, threads, tool, cb)
    try:
        with open(item.path, "rb") as src:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                meter.add(len(chunk))
        finish()
        os.replace(tmp, archive)
        os.utime(archive, (item.st.st_atime, item.st.st_mtime))
    except BaseException as exc:
        try:
            abort()
        except Exception:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError) and not isinstance(exc, ArchiveError):
            message, kind = describe_os_error(exc)
            raise ArchiveError(message, kind=kind, archive=archive) from exc
        raise
    report.files = 1
    report.size = item.st.st_size
    report.packed = os.path.getsize(archive)
    report.outputs.append(archive)
    return report
