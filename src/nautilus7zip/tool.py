# SPDX-License-Identifier: MIT
"""Discovery and capability probing of the 7-Zip command-line program.

``available()`` is cheap and used by the Nautilus extension.  ``probe()``
runs ``7z i`` and is only used by the helper application.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

# Preference order when several builds are installed: "7z" can load plugins
# (e.g. Debian's 7zip-rar), "7zz" is the full standalone build, "7za" only
# handles the major formats and "7zr" only handles .7z.
CANDIDATE_NAMES = ("7z", "7zz", "7za", "7zr")
EXTRA_DIRS = (
    "/usr/bin",
    "/usr/local/bin",
    "/bin",
    "/usr/lib/7zip",
    "/usr/libexec/7zip",
    "/opt/7zip",
    "/snap/bin",
    os.path.expanduser("~/.local/bin"),
)

_quick_cache: tuple[float, str | None, bool] | None = None
_quick_lock = threading.Lock()


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def candidates(override: str | None = None) -> list[str]:
    """Absolute paths of installed 7-Zip executables, best first.

    Setting NAUTILUS_7ZIP_DISABLE=1 hides 7-Zip (handy to try the built-in
    ZIP/TAR support, and used by the test suite).
    """
    if os.environ.get("NAUTILUS_7ZIP_DISABLE"):
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(path: str | None) -> None:
        if not path:
            return
        # /bin is often a symlink to /usr/bin: compare canonical locations.
        key = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        if key not in seen and _is_executable(path):
            seen.add(key)
            found.append(path)

    override = (override or os.environ.get("NAUTILUS_7ZIP_BINARY") or "").strip()
    if override:
        add(os.path.expanduser(override) if os.sep in override else shutil.which(override))
    for name in CANDIDATE_NAMES:
        add(shutil.which(name))
    for directory in EXTRA_DIRS:
        for name in CANDIDATE_NAMES:
            add(os.path.join(directory, name))
    # "7zip.7zz" style snap command names
    add(shutil.which("7zip.7zz"))
    return found


def available(override: str | None = None, ttl: float = 5.0) -> bool:
    """True if any 7-Zip executable is installed (cached for ``ttl`` seconds)."""
    global _quick_cache
    now = time.monotonic()
    with _quick_lock:
        if _quick_cache is not None and now - _quick_cache[0] < ttl and _quick_cache[1] == override:
            return _quick_cache[2]
        result = bool(candidates(override))
        _quick_cache = (now, override, result)
        return result


def only_reduced_builds(override: str | None = None) -> bool:
    """True if the only installed builds are 7zr (7z format only)."""
    names = {os.path.basename(p) for p in candidates(override)}
    return bool(names) and names <= {"7zr"}


@dataclass
class ToolInfo:
    path: str
    version: tuple[int, int] | None = None
    banner: str = ""
    is_p7zip: bool = False
    read_formats: set[str] = field(default_factory=set)  # lower-case 7-Zip handler names
    write_formats: set[str] = field(default_factory=set)
    hashers: set[str] = field(default_factory=set)  # upper-case 7-Zip hasher names
    codecs: set[str] = field(default_factory=set)
    sfx_module: str | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def version_text(self) -> str:
        if self.version is None:
            return "7-Zip"
        return f"7-Zip {self.version[0]}.{self.version[1]:02d}" + (" (p7zip)" if self.is_p7zip else "")

    @property
    def has_rar(self) -> bool:
        """True if RAR archives can actually be decoded (Debian needs 7zip-rar)."""
        if not {"rar", "rar5"} & self.read_formats:
            return False
        return not self.codecs or bool({"RAR3", "RAR5"} & self.codecs)

    def can_read(self, handler: str) -> bool:
        return handler.lower() in self.read_formats

    def can_write(self, handler: str) -> bool:
        return handler.lower() in self.write_formats

    def can_hash(self, sevenzip_names) -> bool:
        return all(n.upper() in self.hashers for n in sevenzip_names)


_VERSION_RE = re.compile(r"7-Zip\s+(?:\([a-z]\)\s+)?(?:\[\d+\]\s+)?(\d+)\.(\d+)")
_probe_cache: dict[tuple[str, float, int], ToolInfo | None] = {}


def _parse_info(path: str, text: str) -> ToolInfo:
    info = ToolInfo(path=path)
    section = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not info.banner and stripped.startswith("7-Zip"):
            info.banner = stripped
            m = _VERSION_RE.search(stripped)
            if m:
                info.version = (int(m.group(1)), int(m.group(2)))
            info.is_p7zip = "p7zip" in stripped
            continue
        if stripped.endswith(":") and stripped[:-1] in ("Libs", "Formats", "Codecs", "Hashers"):
            section = stripped[:-1]
            continue
        if not stripped:
            continue
        if section == "Libs":
            m = re.search(r":\s*(/\S+)$", stripped)
            if m:
                lib_dir = os.path.dirname(m.group(1))
                sfx = os.path.join(lib_dir, "7zCon.sfx")
                if os.path.isfile(sfx):
                    info.sfx_module = sfx
        elif section == "Formats":
            if len(line) < 27:
                continue
            flags = line[3:26]
            rest = line[26:].split()
            if not rest:
                continue
            if len(rest) > 1 and re.fullmatch(r"[a-z.]{4}\d", rest[0]) and "." in rest[0]:
                rest = rest[1:]
            name = rest[0].lower()
            info.read_formats.add(name)
            if flags.lstrip().startswith("C") or flags[:1] == "C":
                info.write_formats.add(name)
        elif section == "Codecs":
            info.codecs.add(stripped.split()[-1].upper())
        elif section == "Hashers":
            info.hashers.add(stripped.split()[-1].upper())
    if info.sfx_module is None:
        real_dir = os.path.dirname(os.path.realpath(path))
        for directory in (real_dir, "/usr/lib/7zip", "/usr/libexec/7zip", "/usr/lib/p7zip", "/usr/libexec/p7zip",
                          "/usr/lib64/p7zip"):
            sfx = os.path.join(directory, "7zCon.sfx")
            if os.path.isfile(sfx):
                info.sfx_module = sfx
                break
    return info


def probe(path: str, timeout: float = 10.0) -> ToolInfo | None:
    """Run ``<path> i`` and parse the supported formats, codecs and hashers."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)
    if key in _probe_cache:
        return _probe_cache[key]
    try:
        proc = subprocess.run(
            [path, "i"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=tool_environment(),
            check=False,
        )
        text = proc.stdout.decode("utf-8", "replace")
        info = _parse_info(path, text) if "Formats:" in text or "7-Zip" in text else None
    except (OSError, subprocess.SubprocessError):
        info = None
    _probe_cache[key] = info
    return info


def tool_environment() -> dict[str, str]:
    """Environment for running 7-Zip: guarantee a UTF-8 locale for file names."""
    env = dict(os.environ)
    ctype = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if "utf-8" not in ctype.lower() and "utf8" not in ctype.lower():
        env["LC_ALL"] = "C.UTF-8"
    return env


def all_tools(override: str | None = None) -> list[ToolInfo]:
    tools = []
    for path in candidates(override):
        info = probe(path)
        if info is not None:
            tools.append(info)
    return tools


def _score(tool: ToolInfo) -> tuple:
    preference = {name: i for i, name in enumerate(CANDIDATE_NAMES)}
    return (
        0 if tool.is_p7zip else 1,
        tool.version or (0, 0),
        len(tool.read_formats),
        -preference.get(tool.name, len(CANDIDATE_NAMES)),
    )


def best_tool(*, read: str | None = None, write: str | None = None, hashers=None,
              override: str | None = None) -> ToolInfo | None:
    """Pick the most capable installed 7-Zip build for a job."""
    tools = all_tools(override)
    if override:
        explicit = [t for t in tools if t.path == os.path.expanduser(override) or t.name == override]
        if explicit:
            tools = explicit + [t for t in tools if t not in explicit]

    def fits(tool: ToolInfo) -> bool:
        if read and not tool.can_read(read):
            return False
        if write and not tool.can_write(write):
            return False
        return not hashers or tool.can_hash(hashers)

    suitable = [t for t in tools if fits(t)]
    if not suitable:
        return None
    if override and suitable[0] in tools[:1]:
        return suitable[0]
    return max(suitable, key=_score)


def default_tool(override: str | None = None) -> ToolInfo | None:
    tools = all_tools(override)
    return max(tools, key=_score) if tools else None


# ---------------------------------------------------------------------------
# Installation hints
# ---------------------------------------------------------------------------


def os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if "=" in line and not line.startswith("#"):
                        key, value = line.rstrip("\n").split("=", 1)
                        data[key] = value.strip().strip('"').strip("'")
            break
        except OSError:
            continue
    return data


def distro_family() -> str:
    """One of "arch", "debian", "fedora", "suse" or "" if unknown."""
    rel = os_release()
    ids = {rel.get("ID", "").lower(), *rel.get("ID_LIKE", "").lower().split()}
    if ids & {"arch", "archlinux", "cachyos", "manjaro", "endeavouros", "garuda"}:
        return "arch"
    if ids & {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "neon"}:
        return "debian"
    if ids & {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine"}:
        return "fedora"
    if ids & {"suse", "opensuse", "opensuse-tumbleweed", "opensuse-leap", "sles"}:
        return "suse"
    return ""


def install_command(rar: bool = False) -> str | None:
    family = distro_family()
    if family == "arch":
        return "sudo pacman -S --needed 7zip"
    if family == "debian":
        return "sudo apt install 7zip 7zip-rar" if rar else "sudo apt install 7zip"
    if family == "fedora":
        return "sudo dnf install 7zip"
    if family == "suse":
        return "sudo zypper install 7zip"
    return None
