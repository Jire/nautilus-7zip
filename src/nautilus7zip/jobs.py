# SPDX-License-Identifier: MIT
"""High-level operations behind the menu items.

Every function here runs in a worker thread and talks to the user only
through a :class:`~nautilus7zip.engines.base.Callbacks` object.

Extraction always happens into a hidden staging folder created next to the
final location and is then moved into place.  That gives every engine the
same conflict handling, makes "Eliminate duplication of root folder" work
even for streamed tarballs, and guarantees that a cancelled or failed
extraction never leaves half-written files behind.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from . import formats, hashalgos, tool
from .engines import engine_for
from .engines.base import (
    ANSWER_TO_MODE,
    Answer,
    ArchiveError,
    ArchiveInfo,
    Callbacks,
    Cancelled,
    ErrorKind,
    FileDescription,
    OverwriteMode,
    PasswordBox,
    Report,
    describe_os_error,
)
from .engines.builtin import TarEngine
from .i18n import _

STAGING_PREFIX = ".7z-extract-"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@dataclass
class ExtractRequest:
    archives: list[str]
    mode: str = "here"  # here | folder | custom
    destination: str | None = None  # custom mode: base folder
    subfolder: bool = False  # custom mode: put each archive in a folder named after it
    eliminate_root: bool = True
    smart_here: bool = False
    overwrite: OverwriteMode = OverwriteMode.ASK
    flatten: bool = False
    password: str | None = None
    members: list[str] | None = None  # extract only these (browser selection)
    strip_prefix: str = ""  # browser: folder the selection lives in
    folder_name: str | None = None  # custom name for the subfolder (single archive)
    password_box: PasswordBox | None = None  # shared by the archive browser to remember a password


@dataclass
class ArchiveOutcome:
    archive: str
    report: Report
    destination: str | None = None
    info: ArchiveInfo | None = None


class Mover:
    """Moves extracted items into place, resolving name conflicts."""

    def __init__(self, cb: Callbacks, mode: OverwriteMode, report: Report) -> None:
        self.cb = cb
        self.mode = mode
        self.report = report

    def _decide(self, existing: str, incoming: str) -> OverwriteMode:
        if self.mode is not OverwriteMode.ASK:
            return self.mode
        answer, apply_all = self.cb.ask_overwrite(FileDescription.from_path(existing),
                                                  FileDescription.from_path(incoming))
        if answer is Answer.CANCEL:
            raise Cancelled()
        mode = ANSWER_TO_MODE[answer]
        if apply_all:
            self.mode = mode
        return mode

    def move_children(self, src_dir: str, dst_dir: str, outputs: list[str] | None = None) -> None:
        for name in sorted(os.listdir(src_dir)):
            self.cb.checkpoint()
            target = self.move(os.path.join(src_dir, name), os.path.join(dst_dir, name))
            if outputs is not None and target is not None:
                outputs.append(target)

    def move(self, src: str, dst: str) -> str | None:
        src_is_dir = os.path.isdir(src) and not os.path.islink(src)
        if not os.path.lexists(dst):
            os.rename(src, dst)
            return dst
        dst_is_dir = os.path.isdir(dst) and not os.path.islink(dst)
        if src_is_dir and dst_is_dir:
            self.move_children(src, dst)  # merge folders, like 7-Zip does
            return dst
        mode = self._decide(dst, src)
        if mode is OverwriteMode.SKIP:
            return None
        if mode is OverwriteMode.RENAME_NEW:
            target = formats.unique_sibling(dst, is_dir=src_is_dir)
            os.rename(src, target)
            return target
        if mode is OverwriteMode.RENAME_EXISTING:
            os.rename(dst, formats.unique_sibling(dst, is_dir=dst_is_dir))
            os.rename(src, dst)
            return dst
        # Replace
        if dst_is_dir:
            # Never delete a whole folder to put a file in its place.
            target = formats.unique_sibling(dst)
            self.report.warnings.append(
                _("“{name}” was saved as “{new}” because a folder has the same name.").format(
                    name=os.path.basename(dst), new=os.path.basename(target)))
            os.rename(src, target)
            return target
        if src_is_dir:
            os.unlink(dst)
        os.rename(src, dst)
        return dst


def _rewrite_staging_links(staging: str, final_root: str) -> None:
    """7-Zip turns absolute link targets into "<output dir>/…"; re-point them."""
    prefix = os.path.realpath(staging).rstrip(os.sep) + os.sep
    for dirpath, dirnames, filenames in os.walk(staging):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                target = os.readlink(path)
                if target.startswith(prefix):
                    os.unlink(path)
                    os.symlink(os.path.join(final_root, target[len(prefix):]), path)


def _sweep_stale_staging(base: str, max_age: float = 12 * 3600) -> None:
    """Remove staging folders left behind by a crashed or killed helper."""
    try:
        names = os.listdir(base)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not name.startswith(STAGING_PREFIX):
            continue
        path = os.path.join(base, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path) and now - os.stat(path).st_mtime > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _make_staging(base: str) -> str:
    _sweep_stale_staging(base)
    try:
        return tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=base)
    except OSError as exc:
        message, kind = describe_os_error(exc, os.path.join(base, "x"))
        raise ArchiveError(message, kind=kind) from exc


def _ensure_folder(path: str) -> tuple[str, bool]:
    """Create ``path`` (or a numbered variant if a file is in the way)."""
    if os.path.isdir(path):
        return path, False
    if os.path.lexists(path):
        path = formats.unique_sibling(path, is_dir=True)
    try:
        os.makedirs(path)
    except OSError as exc:
        message, kind = describe_os_error(exc, path)
        raise ArchiveError(message, kind=kind) from exc
    return path, True


def _remove_if_empty(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


def extract_one(archive: str, request: ExtractRequest, cb: Callbacks, settings=None) -> ArchiveOutcome:
    name = os.path.basename(archive)
    stem = formats.archive_stem(name)
    engine = engine_for(archive, settings)
    password = request.password_box if request.password_box is not None else PasswordBox(request.password)
    info: ArchiveInfo | None = None
    if not isinstance(engine, TarEngine):
        cb.stage(_("Reading “{name}”…").format(name=name))
        info = engine.list(archive, cb, password)
        engine.verify_password(archive, info, cb, password)

    parent = os.path.dirname(os.path.abspath(archive))
    if request.mode == "custom" and request.destination:
        parent = request.destination
    want_folder = request.mode == "folder" or (request.mode == "custom" and request.subfolder)

    created: list[str] = []
    try:
        if request.mode == "custom":
            parent, made = _ensure_folder(parent)
            if made:
                created.append(parent)
        if want_folder:
            folder = (request.folder_name or "").strip().strip("/") or stem
            final, made = _ensure_folder(os.path.join(parent, folder))
            if made:
                created.append(final)
        else:
            final = parent
        staging = _make_staging(final)
    except ArchiveError:
        for path in reversed(created):
            _remove_if_empty(path)
        raise

    report = Report()
    outcome = ArchiveOutcome(archive, report, final, info)
    try:
        cb.stage(_("Extracting “{name}”…").format(name=name))
        total_bytes = info.total_size if info is not None else None
        total_files = info.files if info is not None else None
        members = request.members
        result = engine.extract(archive, staging, cb, password, members=members, flatten=request.flatten,
                                total_bytes=total_bytes, total_files=total_files)
        report.merge(result)
        if report.error_kind is ErrorKind.WRONG_PASSWORD:
            raise ArchiveError(_("The password is incorrect."), kind=ErrorKind.WRONG_PASSWORD, archive=archive,
                               details="\n".join(report.warnings))
        if report.error_kind is ErrorKind.DISK_FULL:
            raise ArchiveError(_("There is not enough space on the disk."), kind=ErrorKind.DISK_FULL,
                               archive=archive)

        cb.stage(_("Moving files into place…"))
        _rewrite_staging_links(staging, final)
        source = staging
        if request.strip_prefix:
            prefix = os.path.join(staging, *request.strip_prefix.strip("/").split("/"))
            if os.path.isdir(prefix):
                source = prefix
        top = sorted(os.listdir(source))
        if want_folder and request.eliminate_root and len(top) == 1 and top[0] == os.path.basename(final) \
                and os.path.isdir(os.path.join(source, top[0])) and not os.path.islink(os.path.join(source, top[0])):
            source = os.path.join(source, top[0])  # "photos.zip" → "photos/photos/…" becomes "photos/…"
            top = sorted(os.listdir(source))
        destination = final
        if not want_folder and request.smart_here and len(top) > 1:
            destination, made = _ensure_folder(os.path.join(final, stem))
            if made:
                created.append(destination)
            outcome.destination = destination
        mover = Mover(cb, request.overwrite, report)
        outputs: list[str] = []
        mover.move_children(source, destination, outputs)
        if want_folder or destination != final:
            report.outputs.append(destination)
        else:
            report.outputs.extend(outputs)
        created.clear()  # extraction succeeded: keep the folders we made
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        for path in reversed(created):
            _remove_if_empty(path)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return outcome


def extract(request: ExtractRequest, cb: Callbacks, settings=None) -> list[ArchiveOutcome]:
    outcomes = []
    for archive in request.archives:
        cb.checkpoint()
        try:
            outcomes.append(extract_one(archive, request, cb, settings))
        except ArchiveError as exc:
            if exc.archive is None:
                exc.archive = archive
            if len(request.archives) == 1 or exc.kind in (ErrorKind.DISK_FULL,):
                raise
            report = Report(errors=[exc.message], error_kind=exc.kind)
            if exc.details:
                report.warnings.append(exc.details)
            outcomes.append(ArchiveOutcome(archive, report))
    return outcomes


# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------


def test(archives: list[str], cb: Callbacks, settings=None) -> list[ArchiveOutcome]:
    outcomes = []
    for archive in archives:
        cb.checkpoint()
        name = os.path.basename(archive)
        try:
            engine = engine_for(archive, settings)
            password = PasswordBox()
            info = None
            if not isinstance(engine, TarEngine):
                cb.stage(_("Reading “{name}”…").format(name=name))
                info = engine.list(archive, cb, password)
                engine.verify_password(archive, info, cb, password)
            cb.stage(_("Testing “{name}”…").format(name=name))
            report = engine.test(archive, cb, password, total_bytes=info.total_size if info else None,
                                 total_files=info.files if info else None)
            if info is not None:
                report.files = report.files or info.files
                report.folders = report.folders or info.folders
                report.size = report.size or info.total_size
            outcomes.append(ArchiveOutcome(archive, report, info=info))
        except ArchiveError as exc:
            report = Report(errors=[exc.message], error_kind=exc.kind)
            if exc.details:
                report.warnings.extend(exc.details.splitlines()[:50])
            outcomes.append(ArchiveOutcome(archive, report))
    return outcomes


# ---------------------------------------------------------------------------
# Creating archives
# ---------------------------------------------------------------------------

# format id → (7-Zip -t type, default extension, builtin writer)
CREATE_FORMATS = {
    "7z": ("7z", ".7z", None),
    "zip": ("zip", ".zip", "zip"),
    "tar": (None, ".tar", "tar"),
    "tar.gz": (None, ".tar.gz", "tar"),
    "tar.bz2": (None, ".tar.bz2", "tar"),
    "tar.xz": (None, ".tar.xz", "tar"),
    "tar.zst": (None, ".tar.zst", "tar"),
    "wim": ("wim", ".wim", None),
    "gz": (None, ".gz", "stream"),
    "bz2": (None, ".bz2", "stream"),
    "xz": (None, ".xz", "stream"),
    "zst": (None, ".zst", "stream"),
}
SINGLE_FILE_FORMATS = ("gz", "bz2", "xz", "zst")
COMPRESSION_OF = {"tar.gz": "gz", "tar.bz2": "bz2", "tar.xz": "xz", "tar.zst": "zst", "gz": "gz", "bz2": "bz2",
                  "xz": "xz", "zst": "zst", "tar": None}


@dataclass
class CreateRequest:
    sources: list[str]
    archive: str
    format: str = "7z"
    level: int = 5
    method: str | None = None
    dictionary: str | None = None  # e.g. "64m"
    word_size: int | None = None
    solid: str | None = None  # None = auto, "off", "on", or a size like "4g"
    threads: int | None = None
    password: str | None = None
    encrypt_names: bool = False
    zip_encryption: str = "AES256"  # or "ZipCrypto"
    volume_size: str | None = None  # e.g. "100m"
    update_mode: str = "add"  # add | update | freshen | sync
    path_mode: str = "relative"  # relative | full | absolute
    store_symlinks: bool = True
    store_hardlinks: bool = False
    sfx: bool = False
    trash_sources: bool = False
    extra_switches: list[str] = field(default_factory=list)


UPDATE_SWITCHES = {
    "add": [],
    "update": ["-up1q1r2x1y2z1w2"],
    "freshen": ["-up1q1r0x1y2z1w2"],
    "sync": ["-up1q0r2x1y2z1w2"],
}


def builtin_can_create(fmt: str) -> bool:
    writer = CREATE_FORMATS.get(fmt, (None, None, None))[2]
    if writer is None:
        return False
    compression = COMPRESSION_OF.get(fmt)
    if compression == "zst":
        return formats.has_python_zstd()
    return True


def create_tool(fmt: str, settings=None) -> tool.ToolInfo | None:
    override = settings.get("sevenzip_path") if settings is not None else None
    sz_type = CREATE_FORMATS.get(fmt, (None, None, None))[0]
    if sz_type is None:
        return None
    return tool.best_tool(write=sz_type, override=override)


def sevenzip_switches(request: CreateRequest, info: tool.ToolInfo) -> list[str]:
    sz_type = CREATE_FORMATS[request.format][0]
    switches = [f"-t{sz_type}"]
    if request.format in ("7z", "zip"):
        switches.append(f"-mx={request.level}")
        if request.method and request.level != 0:
            switches.append(f"-m0={request.method}" if request.format == "7z" else f"-mm={request.method}")
        if request.dictionary and request.level != 0:
            switches.append(f"-md={request.dictionary}")
        if request.word_size and request.level != 0:
            switches.append(f"-mfb={request.word_size}")
        if request.threads:
            switches.append(f"-mmt={request.threads}")
    if request.format == "7z":
        if request.solid:
            switches.append(f"-ms={request.solid}")
        if request.password and request.encrypt_names:
            switches.append("-mhe=on")
        if request.sfx and info.sfx_module:
            switches.append(f"-sfx{info.sfx_module}")
    if request.format == "zip" and request.password:
        switches.append(f"-mem={'ZipCrypto' if request.zip_encryption == 'ZipCrypto' else 'AES256'}")
    if request.volume_size:
        switches.append(f"-v{request.volume_size}")
    switches += UPDATE_SWITCHES.get(request.update_mode, [])
    if request.path_mode == "full":
        switches.append("-spf2")
    elif request.path_mode == "absolute":
        switches.append("-spf")
    if request.store_symlinks:
        switches.append("-snl")
    if request.store_hardlinks:
        switches.append("-snh")
    switches += request.extra_switches
    return switches


def _source_total(sources: list[str]) -> int:
    total = 0
    for source in sources:
        if os.path.isdir(source) and not os.path.islink(source):
            for dirpath, _dirs, files in os.walk(source):
                for name in files:
                    try:
                        total += os.lstat(os.path.join(dirpath, name)).st_size
                    except OSError:
                        pass
        else:
            try:
                total += os.lstat(source).st_size
            except OSError:
                pass
    return total


def create(request: CreateRequest, cb: Callbacks, settings=None) -> Report:
    from .engines import writers
    from .engines.sevenzip import SevenZipEngine

    sources = [os.path.abspath(s) for s in request.sources]
    if not sources:
        raise ArchiveError(_("Nothing to compress."))
    base = formats.common_parent(sources)
    archive = os.path.abspath(request.archive)
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    if archive in sources:
        raise ArchiveError(_("An archive cannot be added to itself."))

    fmt = request.format
    sz_tool = create_tool(fmt, settings)
    compression = COMPRESSION_OF.get(fmt)
    cb.stage(_("Scanning files…"))
    if CREATE_FORMATS[fmt][2] in ("tar", "stream"):
        items = writers.collect(sources, base, cb, follow_links=not request.store_symlinks)
        any_tool = tool.best_tool(override=settings.get("sevenzip_path") if settings is not None else None)
        cb.stage(_("Compressing…"))
        if CREATE_FORMATS[fmt][2] == "stream":
            files = [i for i in items if i.is_file]
            if len(files) != 1 or len(items) != 1:
                raise ArchiveError(_("The {format} format can only compress a single file.").format(
                    format=fmt.upper()))
            report = writers.write_stream(archive, files[0], cb, compression=compression, level=request.level,
                                          threads=request.threads, tool=any_tool)
        else:
            report = writers.write_tar(archive, items, cb, compression=compression, level=request.level,
                                       threads=request.threads, tool=any_tool, store_links=request.store_symlinks)
    elif sz_tool is not None:
        names = [os.path.relpath(s, base) for s in sources]
        if request.path_mode != "relative":
            names = sources
        engine = SevenZipEngine(sz_tool)
        password = PasswordBox(request.password) if request.password else None
        cb.stage(_("Compressing…"))
        report = engine.create(archive, base, names, sevenzip_switches(request, sz_tool), cb, password,
                               total_bytes=_source_total(sources))
        if report.errors:
            raise ArchiveError(report.errors[0], kind=report.error_kind or ErrorKind.GENERIC, archive=archive,
                               details="\n".join(report.warnings))
        if request.volume_size and not os.path.exists(archive) and os.path.exists(archive + ".001"):
            report.outputs.append(archive + ".001")
        else:
            report.outputs.append(archive)
        if request.sfx and fmt == "7z":
            try:
                mode = os.stat(archive).st_mode
                os.chmod(archive, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass
    elif fmt == "zip":
        if request.password:
            raise ArchiveError(_("Creating password-protected archives needs 7-Zip, which is not installed."),
                               kind=ErrorKind.NEEDS_7ZIP)
        if request.volume_size:
            raise ArchiveError(_("Splitting archives into volumes needs 7-Zip, which is not installed."),
                               kind=ErrorKind.NEEDS_7ZIP)
        items = writers.collect(sources, base, cb, follow_links=not request.store_symlinks)
        cb.stage(_("Compressing…"))
        report = writers.write_zip(archive, items, cb, level=request.level, method=request.method,
                                   store_links=request.store_symlinks, update=True)
    else:
        raise ArchiveError(_("Creating {format} archives needs 7-Zip, which is not installed.").format(
            format=fmt.upper()), kind=ErrorKind.NEEDS_7ZIP)

    if request.trash_sources and report.ok:
        cb.stage(_("Moving files to the trash…"))
        report.warnings.extend(trash(sources))
    return report


def trash(paths: list[str]) -> list[str]:
    """Move files to the trash; returns warnings for the ones that failed."""
    warnings = []
    try:
        from gi.repository import Gio
    except ImportError:  # pragma: no cover - the helper always has GObject
        return [_("Could not move the original files to the trash.")]
    for path in paths:
        try:
            Gio.File.new_for_path(path).trash(None)
        except Exception as exc:
            warnings.append(_("Could not move “{name}” to the trash: {error}").format(
                name=os.path.basename(path), error=getattr(exc, "message", str(exc))))
    return warnings


def email_archive_path(paths: list[str], filename: str) -> str:
    """Where "Compress and Email" puts the archive.

    With a real mail client it goes to a private cache folder (the client
    copies the attachment).  With webmail the user has to attach it by hand,
    so it is saved next to the originals, under a name that is not taken.
    """
    _client, attaches = mail_client()
    if attaches:
        return os.path.join(email_staging_dir(), filename)
    return formats.unique_sibling(os.path.join(formats.common_parent(paths), filename))


def email_staging_dir() -> str:
    from .settings import cache_dir

    root = os.path.join(cache_dir(), "email")
    os.makedirs(root, exist_ok=True)
    # Forget attachments older than a week.
    now = time.time()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if now - os.stat(path).st_mtime > 7 * 86400:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    return tempfile.mkdtemp(prefix="mail-", dir=root)


MAIL_CLIENTS = ("thunderbird", "betterbird", "evolution", "geary", "kmail", "claws", "mailspring", "sylpheed",
                "balsa", "trojita", "seamonkey", "mutt")


def mail_client() -> tuple[str | None, bool]:
    """(name of the default mailto: handler, whether it can take attachments).

    Web browsers handle mailto: links for webmail, but cannot attach files.
    """
    try:
        from gi.repository import Gio
    except ImportError:  # pragma: no cover
        return None, False
    info = Gio.AppInfo.get_default_for_uri_scheme("mailto")
    if info is None:
        return None, False
    ident = f"{info.get_id() or ''} {info.get_executable() or ''}".lower()
    return info.get_display_name(), any(client in ident for client in MAIL_CLIENTS)


def send_email(path: str) -> bool:
    """Open the e-mail composer with ``path`` attached (xdg-email).

    Runs in a worker thread.  xdg-email exits quickly when it hands the
    message to a running mail client; when it starts one in the foreground it
    keeps running, which also means success.
    """
    exe = shutil.which("xdg-email")
    if exe is None:
        return False
    try:
        proc = subprocess.Popen([exe, "--attach", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    try:
        return proc.wait(timeout=8) == 0
    except subprocess.TimeoutExpired:
        return True


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


@dataclass
class ChecksumRow:
    path: str  # absolute
    name: str  # relative display name
    size: int | None
    digests: dict[str, str]
    is_dir: bool = False
    error: str | None = None


@dataclass
class ChecksumResult:
    rows: list[ChecksumRow] = field(default_factory=list)
    algorithms: list[str] = field(default_factory=list)
    data_sums: dict[str, str] = field(default_factory=dict)
    names_sums: dict[str, str] = field(default_factory=dict)
    total_size: int = 0
    engine: str = ""
    warnings: list[str] = field(default_factory=list)


def compute_checksums(paths: list[str], algorithm_ids: list[str], cb: Callbacks, settings=None) -> ChecksumResult:
    from .engines.sevenzip import SevenZipEngine

    algorithms = [hashalgos.BY_ID[a] for a in algorithm_ids]
    paths = [os.path.abspath(p) for p in paths]
    base = formats.common_parent(paths)
    result = ChecksumResult(algorithms=[a.id for a in algorithms])
    cb.stage(_("Scanning files…"))
    total = _source_total(paths)
    result.total_size = total
    override = settings.get("sevenzip_path") if settings is not None else None
    sz = tool.best_tool(hashers=[a.sevenzip for a in algorithms], override=override)
    python_ok = all(a.builtin for a in algorithms)
    if sz is not None:
        result.engine = sz.version_text
        names = [os.path.relpath(p, base) for p in paths]
        cb.stage(_("Calculating checksums…"))
        table = SevenZipEngine(sz).hash(base, names, algorithms, cb, total_bytes=total)
        for row in table.rows:
            result.rows.append(ChecksumRow(os.path.join(base, row.name), row.name, row.size, row.digests,
                                           row.is_dir))
        result.data_sums = table.data_sums
        result.names_sums = table.names_sums
        result.warnings = table.warnings
        return result
    if not python_ok:
        missing = ", ".join(a.label for a in algorithms if not a.builtin)
        raise ArchiveError(_("Calculating {algorithms} needs 7-Zip, which is not installed.").format(
            algorithms=missing), kind=ErrorKind.NEEDS_7ZIP)
    result.engine = _("Built-in")
    cb.stage(_("Calculating checksums…"))
    done = 0
    last = 0.0
    files: list[tuple[str, str]] = []
    for path in paths:
        if os.path.isdir(path) and not os.path.islink(path):
            result.rows.append(ChecksumRow(path, os.path.relpath(path, base), None, {}, True))
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for d in dirnames:
                    full = os.path.join(dirpath, d)
                    result.rows.append(ChecksumRow(full, os.path.relpath(full, base), None, {}, True))
                for f in sorted(filenames):
                    full = os.path.join(dirpath, f)
                    files.append((full, os.path.relpath(full, base)))
        else:
            files.append((path, os.path.relpath(path, base)))
    for full, rel in files:
        cb.checkpoint()
        if os.path.islink(full) and not os.path.exists(full):
            continue
        hashers = {a.id: hashalgos.new_python_hasher(a.python) for a in algorithms}
        size = 0
        try:
            with open(full, "rb") as fh:
                while True:
                    cb.checkpoint()
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    done += len(chunk)
                    for h in hashers.values():
                        h.update(chunk)
                    now = time.monotonic()
                    if now - last > 0.08:
                        last = now
                        cb.progress(done / total if total else None, current=rel, done_bytes=done,
                                    total_bytes=total)
        except OSError as exc:
            result.rows.append(ChecksumRow(full, rel, None, {}, error=exc.strerror or str(exc)))
            continue
        digests = {aid: hashalgos.format_digest(aid, h.hexdigest()) for aid, h in hashers.items()}
        result.rows.append(ChecksumRow(full, rel, size, digests))
    result.rows.sort(key=lambda r: r.name)
    return result


def write_checksum_file(result: ChecksumResult, algorithm_id: str, target: str) -> str:
    """Save "<digest>  <name>" lines (GNU coreutils format, *sum -c compatible)."""
    base = os.path.dirname(os.path.abspath(target))
    lines = []
    for row in result.rows:
        if row.is_dir or algorithm_id not in row.digests:
            continue
        name = os.path.relpath(row.path, base).replace(os.sep, "/")
        digest = row.digests[algorithm_id].lower() if algorithm_id != "crc32" else row.digests[algorithm_id]
        if algorithm_id == "crc32":
            lines.append(f"{name} {digest}")
        elif "\n" in name or "\\" in name:
            escaped = name.replace("\\", "\\\\").replace("\n", "\\n")
            lines.append(f"\\{digest}  {escaped}")
        else:
            lines.append(f"{digest}  {name}")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return target


# ---------------------------------------------------------------------------
# Verifying checksum files
# ---------------------------------------------------------------------------


@dataclass
class VerifyRow:
    name: str
    path: str
    algorithm: str
    expected: str
    actual: str | None = None
    status: str = "pending"  # ok | failed | missing | error
    error: str | None = None


def parse_checksum_file(path: str) -> list[VerifyRow]:
    import re

    _is_checksum, hint = formats.checksum_file_algorithm(os.path.basename(path))
    base = os.path.dirname(os.path.abspath(path))
    rows: list[VerifyRow] = []
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith(("#", ";")):
                continue
            m = re.match(r"^(\w[\w-]*)\s*\((.+)\)\s*=\s*([0-9A-Fa-f]+)\s*$", line)  # BSD style
            if m:
                algorithm = m.group(1).lower().replace("_", "-")
                algorithm = {"sha2-256": "sha256", "blake2b-512": "blake2b"}.get(algorithm, algorithm)
                name, digest = m.group(2), m.group(3)
            else:
                m = re.match(r"^(\\?)([0-9A-Fa-f]{8,})[ \t]+\*?(.+)$", line)  # GNU style
                if m:
                    digest, name = m.group(2), m.group(3)
                    if m.group(1):
                        name = name.replace("\\n", "\n").replace("\\\\", "\\")
                    algorithm = hint or hashalgos.LENGTH_HINTS.get(len(digest), "")
                else:
                    m = re.match(r"^(.+?)\s+([0-9A-Fa-f]{8})$", line)  # SFV style
                    if not m:
                        continue
                    name, digest, algorithm = m.group(1), m.group(2), "crc32"
            if hint == "blake2b" and algorithm == "sha512":
                algorithm = "blake2b"
            rows.append(VerifyRow(name, os.path.join(base, name), algorithm, digest.lower()))
    return rows


def verify_checksums(path: str, cb: Callbacks) -> list[VerifyRow]:
    rows = parse_checksum_file(path)
    if not rows:
        raise ArchiveError(_("No checksums were found in “{name}”.").format(name=os.path.basename(path)))
    total = 0
    for row in rows:
        try:
            total += os.path.getsize(row.path)
        except OSError:
            pass
    done = 0
    last = 0.0
    for row in rows:
        cb.checkpoint()
        python_name = hashalgos.VERIFY_ALGORITHMS.get(row.algorithm)
        if python_name is None:
            row.status = "error"
            row.error = _("Unknown algorithm “{name}”.").format(name=row.algorithm or "?")
            continue
        if not os.path.exists(row.path):
            row.status = "missing"
            continue
        hasher = hashalgos.new_python_hasher(python_name)
        try:
            with open(row.path, "rb") as fh:
                while True:
                    cb.checkpoint()
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.08:
                        last = now
                        cb.progress(done / total if total else None, current=row.name, done_bytes=done,
                                    total_bytes=total)
        except OSError as exc:
            row.status = "error"
            row.error = exc.strerror or str(exc)
            continue
        row.actual = hasher.hexdigest().lower()
        row.status = "ok" if row.actual == row.expected else "failed"
    return rows
