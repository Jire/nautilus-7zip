# SPDX-License-Identifier: MIT
"""Archive format catalog, detection and naming rules.

This module is pure Python (no GObject imports) because it is shared by the
Nautilus extension, which runs inside the file manager process, and by the
helper application.  Everything here must stay fast: the extension calls it
every time a context menu is opened.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

# Categories decide whether the 7-Zip extraction items are offered for a file.
# Real archives always get them; the other categories can be switched off in
# the preferences (7-Zip on Windows shows them for all of these by default).
ARCHIVE = "archive"
DISK = "disk"  # .iso, .img, .dmg, .vhd(x), .qcow2 …
PACKAGE = "package"  # .deb, .rpm, .jar, .apk, .msi, .exe …
DOCUMENT = "document"  # .docx, .odt, .epub … (ZIP containers)

CATEGORIES = (ARCHIVE, DISK, PACKAGE, DOCUMENT)

# Built-in (pure Python) reader families used when 7-Zip is not installed.
ZIP = "zip"
TAR = "tar"
STREAM = "stream"


@dataclass(frozen=True)
class Format:
    id: str
    label: str
    category: str
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...] = ()
    builtin: str | None = None  # ZIP / TAR / STREAM, or None if 7-Zip is required
    compression: str | None = None  # gz, bz2, xz, zst, lzma, Z (TAR and STREAM only)

    @property
    def needs_7zip(self) -> bool:
        """True if this format can only be read with 7-Zip installed."""
        if self.builtin is None:
            return True
        if self.compression is None:
            return False
        return self.compression not in python_compressions()


def _f(id, label, category, exts, mimes=(), builtin=None, compression=None) -> Format:
    return Format(id, label, category, tuple(exts), tuple(mimes), builtin, compression)


FORMATS: tuple[Format, ...] = (
    # --- Archives -----------------------------------------------------------
    _f("7z", "7z", ARCHIVE, ["7z"], ["application/x-7z-compressed"]),
    _f("zip", "ZIP", ARCHIVE, ["zip", "zipx"],
       ["application/zip", "application/x-zip", "application/x-zip-compressed", "application/x-zipx"], ZIP),
    _f("rar", "RAR", ARCHIVE, ["rar"],
       ["application/vnd.rar", "application/x-rar", "application/x-rar-compressed"]),
    _f("tar", "TAR", ARCHIVE, ["tar"], ["application/x-tar", "application/x-gtar"], TAR),
    _f("tar.gz", "TAR.GZ", ARCHIVE, ["tar.gz", "tgz", "tpz"],
       ["application/x-compressed-tar"], TAR, "gz"),
    _f("tar.bz2", "TAR.BZ2", ARCHIVE, ["tar.bz2", "tar.bz", "tbz2", "tbz", "tb2"],
       ["application/x-bzip2-compressed-tar", "application/x-bzip-compressed-tar",
        "application/x-bzip1-compressed-tar"], TAR, "bz2"),
    _f("tar.xz", "TAR.XZ", ARCHIVE, ["tar.xz", "txz"], ["application/x-xz-compressed-tar"], TAR, "xz"),
    _f("tar.zst", "TAR.ZST", ARCHIVE, ["tar.zst", "tar.zstd", "tzst"],
       ["application/x-zstd-compressed-tar"], TAR, "zst"),
    _f("tar.lzma", "TAR.LZMA", ARCHIVE, ["tar.lzma", "tlz"], ["application/x-lzma-compressed-tar"], TAR, "lzma"),
    _f("tar.Z", "TAR.Z", ARCHIVE, ["tar.z", "taz"], ["application/x-tarz"], TAR, "Z"),
    _f("gz", "GZIP", ARCHIVE, ["gz", "gzip"], ["application/gzip", "application/x-gzip"], STREAM, "gz"),
    _f("bz2", "BZIP2", ARCHIVE, ["bz2", "bzip2", "bz"], ["application/x-bzip2", "application/x-bzip"], STREAM, "bz2"),
    _f("xz", "XZ", ARCHIVE, ["xz"], ["application/x-xz"], STREAM, "xz"),
    _f("zst", "Zstandard", ARCHIVE, ["zst", "zstd"], ["application/zstd", "application/x-zstd"], STREAM, "zst"),
    _f("lzma", "LZMA", ARCHIVE, ["lzma"], ["application/x-lzma"], STREAM, "lzma"),
    _f("Z", "Z", ARCHIVE, ["z"], ["application/x-compress"]),
    _f("cab", "CAB", ARCHIVE, ["cab"], ["application/vnd.ms-cab-compressed"]),
    _f("arj", "ARJ", ARCHIVE, ["arj"], ["application/x-arj"]),
    _f("lzh", "LZH", ARCHIVE, ["lzh", "lha"], ["application/x-lha", "application/x-lzh-compressed"]),
    _f("cpio", "CPIO", ARCHIVE, ["cpio"], ["application/x-cpio"]),
    _f("wim", "WIM", ARCHIVE, ["wim", "swm", "esd"], ["application/x-ms-wim"]),
    _f("xar", "XAR", ARCHIVE, ["xar"], ["application/x-xar"]),
    _f("cbz", "CBZ", ARCHIVE, ["cbz"], ["application/vnd.comicbook+zip", "application/x-cbz"], ZIP),
    _f("cbr", "CBR", ARCHIVE, ["cbr"], ["application/vnd.comicbook-rar", "application/x-cbr"]),
    _f("cb7", "CB7", ARCHIVE, ["cb7"], ["application/x-cb7"]),
    _f("cbt", "CBT", ARCHIVE, ["cbt"], ["application/x-cbt"], TAR),
    # --- Disk images ----------------------------------------------------------
    _f("iso", "ISO", DISK, ["iso"],
       ["application/x-iso9660-image", "application/x-cd-image", "application/vnd.efi.iso"]),
    _f("img", "Disk image", DISK, ["img"], ["application/x-raw-disk-image", "application/vnd.efi.img"]),
    _f("dmg", "DMG", DISK, ["dmg"], ["application/x-apple-diskimage"]),
    _f("vhd", "VHD", DISK, ["vhd"], ["application/x-vhd-disk"]),
    _f("vhdx", "VHDX", DISK, ["vhdx", "avhdx"], ["application/x-vhdx-disk"]),
    _f("vmdk", "VMDK", DISK, ["vmdk"], ["application/x-vmdk-disk"]),
    _f("vdi", "VDI", DISK, ["vdi"], ["application/x-virtualbox-vdi", "application/x-vdi-disk"]),
    _f("qcow", "QCOW", DISK, ["qcow2", "qcow", "qcow2c"], ["application/x-qemu-disk"]),
    _f("squashfs", "SquashFS", DISK, ["squashfs", "sqfs"], ["application/vnd.squashfs", "application/x-squashfs"]),
    # --- Packages & installers ------------------------------------------------
    _f("deb", "DEB", PACKAGE, ["deb", "udeb"],
       ["application/vnd.debian.binary-package", "application/x-deb", "application/x-debian-package"]),
    _f("rpm", "RPM", PACKAGE, ["rpm"], ["application/x-rpm", "application/x-redhat-package-manager"]),
    _f("jar", "JAR", PACKAGE, ["jar", "war", "ear"],
       ["application/x-java-archive", "application/java-archive", "application/x-jar"], ZIP),
    _f("apk", "APK", PACKAGE, ["apk", "aab", "xapk", "apks"], ["application/vnd.android.package-archive"], ZIP),
    _f("xpi", "XPI", PACKAGE, ["xpi"], ["application/x-xpinstall"], ZIP),
    _f("crx", "CRX", PACKAGE, ["crx"], ["application/x-chrome-extension"], ZIP),
    _f("ipa", "IPA", PACKAGE, ["ipa"], ["application/x-ios-app", "application/x-itunes-ipa"], ZIP),
    _f("appx", "APPX", PACKAGE, ["appx", "msix", "appxbundle", "msixbundle"],
       ["application/vnd.ms-appx", "application/msix"], ZIP),
    _f("whl", "Wheel", PACKAGE, ["whl"], ["application/x-wheel+zip"], ZIP),
    _f("nupkg", "NuGet", PACKAGE, ["nupkg", "vsix"], [], ZIP),
    _f("msi", "MSI", PACKAGE, ["msi", "msp", "msm"], ["application/x-msi", "application/x-ole-storage"]),
    _f("exe", "EXE", PACKAGE, ["exe"],
       ["application/x-ms-dos-executable", "application/x-msdownload", "application/x-msdos-program",
        "application/vnd.microsoft.portable-executable", "application/x-dosexec"]),
    _f("pkg", "PKG", PACKAGE, ["pkg"], []),
    _f("snap", "Snap", PACKAGE, ["snap"], ["application/vnd.snap", "application/x-snap"]),
    # --- Documents that are really ZIP containers ----------------------------
    _f("ooxml", "Office document", DOCUMENT,
       ["docx", "docm", "dotx", "xlsx", "xlsm", "xltx", "pptx", "pptm", "potx", "ppsx", "vsdx"],
       ["application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-word.document.macroenabled.12",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.ms-powerpoint.presentation.macroenabled.12"], ZIP),
    _f("odf", "OpenDocument", DOCUMENT,
       ["odt", "ods", "odp", "odg", "odf", "odc", "odb", "ott", "ots", "otp", "otg"],
       ["application/vnd.oasis.opendocument.text", "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation", "application/vnd.oasis.opendocument.graphics",
        "application/vnd.oasis.opendocument.formula", "application/vnd.oasis.opendocument.database"], ZIP),
    _f("epub", "EPUB", DOCUMENT, ["epub"], ["application/epub+zip"], ZIP),
    _f("xps", "XPS", DOCUMENT, ["xps", "oxps"], ["application/oxps", "application/vnd.ms-xpsdocument"], ZIP),
    _f("chm", "CHM", DOCUMENT, ["chm"], ["application/vnd.ms-htmlhelp", "application/x-chm"]),
)

_BY_ID = {f.id: f for f in FORMATS}
_BY_MIME: dict[str, Format] = {}
_BY_EXT: dict[str, Format] = {}
for _fmt in FORMATS:
    for _m in _fmt.mime_types:
        _BY_MIME.setdefault(_m, _fmt)
    for _e in _fmt.extensions:
        _BY_EXT.setdefault(_e, _fmt)
# Longest suffixes first so that "tar.gz" wins over "gz".
_EXT_SUFFIXES = sorted(_BY_EXT, key=len, reverse=True)

# Generic ZIP-based formats we have no specific entry for (e.g. Krita files)
# are treated as documents when the MIME database says they derive from ZIP.
_GENERIC_ZIP = _f("zip-container", "ZIP container", DOCUMENT, [], [], ZIP)


def by_id(format_id: str) -> Format:
    return _BY_ID[format_id]


_python_compressions: frozenset[str] | None = None


def python_compressions() -> frozenset[str]:
    """Compression filters the built-in (Python) engine can decode."""
    global _python_compressions
    if _python_compressions is None:
        found = set()
        for name, module in (("gz", "zlib"), ("bz2", "bz2"), ("xz", "lzma"), ("lzma", "lzma")):
            if _importable(module):
                found.add(name)
        if _importable("compression.zstd"):
            found.add("zst")
        _python_compressions = frozenset(found)
    return _python_compressions


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def has_python_zstd() -> bool:
    return "zst" in python_compressions()


# ---------------------------------------------------------------------------
# Multi-volume archives
# ---------------------------------------------------------------------------

_RE_RAR_PART = re.compile(r"^(?P<base>.+)\.part(?P<num>\d+)\.rar$", re.IGNORECASE)
_RE_NUMBERED = re.compile(r"^(?P<base>.+?)(?P<inner>\.[A-Za-z0-9]{1,8})?\.(?P<num>\d{3,})$")
_RE_RAR_OLD = re.compile(r"^(?P<base>.+)\.r(?P<num>\d{2,3})$", re.IGNORECASE)
_RE_ZIP_SPLIT = re.compile(r"^(?P<base>.+)\.z(?P<num>\d{2,3})$", re.IGNORECASE)


@dataclass(frozen=True)
class Volume:
    """Describes a file that belongs to a multi-volume set."""

    key: str  # identifies the set (directory-independent)
    index: int  # position in the set (first volume has the lowest index)
    first: tuple[str, ...]  # candidate file names of the first volume
    inner: str  # name of the archive the volumes form, e.g. "backup.7z"


def volume_of(name: str) -> Volume | None:
    """Return volume information if ``name`` looks like part of a split archive."""
    m = _RE_RAR_PART.match(name)
    if m:
        num = m.group("num")
        base = m.group("base")
        firsts = tuple(dict.fromkeys(f"{base}.part{str(1).zfill(w)}.rar" for w in (len(num), 1, 2, 3)))
        return Volume(f"{base}.rar#part".lower(), int(num), firsts, f"{base}.rar")
    m = _RE_NUMBERED.match(name)
    if m:
        num = m.group("num")
        inner = (m.group("inner") or "")
        base = m.group("base") + inner
        return Volume(f"{base}#split".lower(), int(num), (f"{base}.{str(1).zfill(len(num))}",), base)
    m = _RE_RAR_OLD.match(name)
    if m:
        base = m.group("base")
        return Volume(f"{base}.rar#r".lower(), int(m.group("num")) + 1, (f"{base}.rar",), f"{base}.rar")
    m = _RE_ZIP_SPLIT.match(name)
    if m:
        base = m.group("base")
        return Volume(f"{base}.zip#z".lower(), int(m.group("num")), (f"{base}.zip",), f"{base}.zip")
    return None


def volume_key_for_first(name: str) -> str | None:
    """Keys under which a *first* volume (x.rar / x.zip) may group with its parts."""
    lower = name.lower()
    if lower.endswith(".rar") and not _RE_RAR_PART.match(name):
        return f"{name[:-4]}.rar#r".lower()
    if lower.endswith(".zip"):
        return f"{name[:-4]}.zip#z".lower()
    return None


def first_volume_path(path: str) -> str:
    """Map any volume of a split archive to the volume 7-Zip must be given."""
    directory, name = os.path.split(path)
    vol = volume_of(name)
    if vol is None:
        return path
    for candidate in vol.first:
        candidate_path = os.path.join(directory, candidate)
        if os.path.exists(candidate_path):
            return candidate_path
    return path


def dedupe_volumes(paths: list[str]) -> list[str]:
    """Collapse several volumes of the same split archive into one entry.

    The order of first appearance is kept and every set is represented by
    its first volume (falling back to the lowest selected volume).
    """
    slots: list[tuple[str, str]] = []  # ("path", path) or ("set", key)
    best: dict[str, tuple[int, str]] = {}
    for path in paths:
        directory, name = os.path.split(path)
        vol = volume_of(name)
        if vol is not None:
            key = os.path.join(directory, vol.key)
            first = first_volume_path(path)
            index, chosen = (-1, first) if first != path else (vol.index, path)
        else:
            set_key = volume_key_for_first(name)
            if set_key is None:
                slots.append(("path", path))
                continue
            key, index, chosen = os.path.join(directory, set_key), -1, path
        if key not in best:
            slots.append(("set", key))
            best[key] = (index, chosen)
        elif index < best[key][0]:
            best[key] = (index, chosen)
    return [value if kind == "path" else best[value][1] for kind, value in slots]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def split_extension(name: str) -> tuple[str, Format | None]:
    """Split a file name into (stem, format) using the longest known suffix."""
    lower = name.lower()
    for ext in _EXT_SUFFIXES:
        if lower.endswith("." + ext) and len(lower) > len(ext) + 1:
            return name[: -(len(ext) + 1)], _BY_EXT[ext]
    return name, None


def detect(name: str, mime_type: str | None = None, *, is_a=None) -> Format | None:
    """Detect the archive format of a file from its name and MIME type.

    ``is_a`` is an optional ``callable(mime, parent_mime) -> bool`` (for
    example ``Gio.content_type_is_a``) used to recognise ZIP-derived types
    that are not in the catalog.
    """
    vol = volume_of(name)
    if vol is not None:
        _stem, fmt = split_extension(vol.inner)
        if fmt is not None:
            return fmt
        if vol.index == 1 and vol.key.endswith("#split"):
            return _SPLIT  # 7-Zip's generic "Split" handler (name.001, name.002 …)
    _stem, fmt = split_extension(name)
    if fmt is not None:
        return fmt
    if mime_type:
        fmt = _BY_MIME.get(mime_type)
        if fmt is not None:
            return fmt
        if is_a is not None:
            try:
                if is_a(mime_type, "application/zip"):
                    return _GENERIC_ZIP
            except Exception:
                pass
    return None


_SPLIT = _f("split", "Split", ARCHIVE, ["001"], [])


def archive_stem(name: str) -> str:
    """The folder name used by "Extract to “name/”" for an archive file name."""
    vol = volume_of(name)
    if vol is not None:
        name = vol.inner
    stem, fmt = split_extension(name)
    if fmt is None and vol is None:
        stem = os.path.splitext(name)[0] if not name.startswith(".") else name
    stem = stem.strip()
    if not stem or stem in (".", ".."):
        stem = name if name not in ("", ".", "..") else "archive"
    return stem


def archive_name_for(paths: list[str], directories: set[str] | None = None) -> str:
    """Default archive base name (without extension) for "Add to …" items.

    A single file loses its extension ("report.pdf" → "report"), a single
    folder keeps its name and several items are named after their folder,
    which matches 7-Zip's behaviour.  ``directories`` lists the paths known
    to be folders (avoids a stat() when the caller already knows).
    """
    if len(paths) == 1:
        path = paths[0].rstrip(os.sep) or paths[0]
        name = os.path.basename(path)
        is_dir = path in directories if directories is not None else os.path.isdir(path)
        if is_dir:
            return name or "Archive"
        stem, fmt = split_extension(name)  # "app-1.0.tar.gz" → "app-1.0", not "app-1.0.tar"
        if fmt is None:
            stem = os.path.splitext(name)[0]
        return stem or name or "Archive"
    parent = common_parent(paths)
    name = os.path.basename(parent.rstrip(os.sep))
    return name or "Archive"


def common_parent(paths: list[str]) -> str:
    """The deepest directory that contains every path."""
    parents = [os.path.dirname(os.path.abspath(p.rstrip(os.sep) or p)) for p in paths]
    if not parents:
        return os.getcwd()
    try:
        return os.path.commonpath(parents)
    except ValueError:
        return parents[0]


# ---------------------------------------------------------------------------
# Checksum files
# ---------------------------------------------------------------------------

CHECKSUM_EXTENSIONS = {
    "md5": "md5", "md5sum": "md5",
    "sha1": "sha1", "sha1sum": "sha1",
    "sha224": "sha224", "sha224sum": "sha224",
    "sha256": "sha256", "sha256sum": "sha256",
    "sha384": "sha384", "sha384sum": "sha384",
    "sha512": "sha512", "sha512sum": "sha512",
    "sha3": None, "sha2": None,
    "b2": "blake2b", "blake2": "blake2b",
    "sfv": "crc32",
    "crc32": "crc32",
}
CHECKSUM_NAMES = {
    "md5sums": "md5", "sha1sums": "sha1", "sha224sums": "sha224", "sha256sums": "sha256",
    "sha384sums": "sha384", "sha512sums": "sha512", "b2sums": "blake2b",
    "checksums": None, "checksum": None, "checksums.txt": None, "sha256sums.txt": "sha256",
    "sha512sums.txt": "sha512", "sha1sums.txt": "sha1", "md5sums.txt": "md5",
}


def checksum_file_algorithm(name: str) -> tuple[bool, str | None]:
    """Return (is_checksum_file, algorithm hint) for a file name."""
    lower = name.lower()
    if lower in CHECKSUM_NAMES:
        return True, CHECKSUM_NAMES[lower]
    base, ext = os.path.splitext(lower)
    ext = ext[1:]
    if ext in CHECKSUM_EXTENSIONS and base:
        return True, CHECKSUM_EXTENSIONS[ext]
    if ext == "txt" and base in CHECKSUM_NAMES:
        return True, CHECKSUM_NAMES[base]
    return False, None


# ---------------------------------------------------------------------------
# Small helpers shared by the menu and the helper application
# ---------------------------------------------------------------------------


def ellipsize_middle(text: str, limit: int = 40) -> str:
    """Shorten long names for menu labels while keeping both ends readable."""
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = (keep + 1) // 2
    tail = keep - head
    return text[:head] + "…" + text[len(text) - tail:]


def unique_sibling(path: str, *, is_dir: bool = False) -> str:
    """A non-existing path next to ``path``, numbered the way 7-Zip does it.

    "report.pdf" → "report_1.pdf", "photos.tar.gz" → "photos_1.tar.gz",
    folders simply get the suffix appended ("v1.2" → "v1.2_1").
    """
    if not os.path.lexists(path):
        return path
    directory, name = os.path.split(path)
    if is_dir:
        stem, ext = name, ""
    else:
        stem, fmt = split_extension(name)
        if fmt is not None:
            ext = name[len(stem):]
        else:
            stem, ext = os.path.splitext(name)
    for i in range(1, 1_000_000):
        candidate = os.path.join(directory, f"{stem}_{i}{ext}")
        if not os.path.lexists(candidate):
            return candidate
    raise FileExistsError(path)


def is_local_path(path: str | None) -> bool:
    return bool(path) and os.path.isabs(path)


def python_version_ok() -> bool:
    return sys.version_info >= (3, 10)
