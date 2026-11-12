# SPDX-License-Identifier: MIT
"""Engine that drives the 7-Zip command-line program.

7-Zip is run with all three output streams redirected to pipes:

* ``-bsp1``  progress goes to stdout as "NN% count op name" snapshots that are
  erased with backspaces, which we turn into progress callbacks;
* ``-bso1``  regular messages (and interactive prompts) go to stdout;
* ``-bse2``  errors go to stderr.

Passwords are never put on the command line (where other users could read
them from ``/proc``): 7-Zip prompts for them on stdin and we answer.
"""

from __future__ import annotations

import codecs
import datetime as _dt
import fcntl
import os
import re
import select
import signal
import subprocess
import time
from dataclasses import dataclass, field

from .. import formats, hashalgos
from ..i18n import _
from ..tool import ToolInfo, tool_environment
from .base import (
    Answer,
    ArchiveError,
    ArchiveInfo,
    Callbacks,
    Cancelled,
    Entry,
    ErrorKind,
    FileDescription,
    PasswordBox,
    Report,
)

COMMON = ["-sccUTF-8", "-bso1", "-bse2", "-bsp1"]

_PROGRESS_RE = re.compile(r"^\s*(\d{1,3})%(?:\s+(\d+))?(?:\s+([-+=URDT.])(?=\s)\s+|\s+)?(.*)$")
_PASSWORD_PROMPT_RE = re.compile(r"(?:Enter|Verify|Reenter|Re-enter) password[^\n:]*:\s*$", re.IGNORECASE)
_OVERWRITE_PROMPT_RE = re.compile(r"\(Q\)uit\?\s*$")
_ADD_TOTAL_RE = re.compile(r"Add new data to archive:.*?(\d+) bytes")
_TOKEN_RE = re.compile(r"(\x08+|\r+|\n+)")


@dataclass
class RunResult:
    returncode: int
    lines: list[str] = field(default_factory=list)
    stderr: str = ""
    password_prompts: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n" + self.stderr

    def error_lines(self) -> list[str]:
        out = []
        for raw in self.stderr.splitlines():
            line = raw.strip()
            if line and line not in out:
                out.append(line)
        return out


class _Terminal:
    """Turns 7-Zip's backspace-animated output into lines and progress."""

    def __init__(self, on_line, on_progress) -> None:
        self.pending = ""
        self.on_line = on_line
        self.on_progress = on_progress

    def feed(self, text: str) -> None:
        if "\b" not in text and "\r" not in text:
            parts = text.split("\n")
            if len(parts) == 1:
                self.pending += text
                return
            parts[0] = self.pending + parts[0]
            self.pending = parts.pop()
            for line in parts:
                self._line(line)
            return
        for token in _TOKEN_RE.split(text):
            if not token:
                continue
            first = token[0]
            if first == "\n":
                for _i in range(len(token)):
                    line, self.pending = self.pending, ""
                    self._line(line)
            elif first in "\b\r" and token.strip("\b\r") == "":
                if self.pending.strip():
                    self._snapshot(self.pending)
                self.pending = ""
            else:
                self.pending += token

    def _line(self, line: str) -> None:
        if "%" in line[:6] and _PROGRESS_RE.match(line):
            self._snapshot(line)
        else:
            self.on_line(line.rstrip("\r"))

    def _snapshot(self, text: str) -> None:
        m = _PROGRESS_RE.match(text)
        if m:
            count = int(m.group(2)) if m.group(2) else None
            self.on_progress(int(m.group(1)), count, (m.group(4) or "").strip() or None)


class SevenZipRunner:
    """Runs one 7-Zip command and services its prompts."""

    def __init__(self, tool: ToolInfo, args: list[str], cb: Callbacks, *, cwd: str | None = None,
                 password: PasswordBox | None = None, archive_name: str = "", total_bytes: int | None = None,
                 total_files: int | None = None, packed_path: str | None = None) -> None:
        self.argv = [tool.path, *args]
        self.cb = cb
        self.cwd = cwd
        self.password = password
        self.archive_name = archive_name
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.packed_path = packed_path
        self.result = RunResult(-1)
        self._last_packed_check = 0.0
        self._prompt_lines: list[str] = []

    # -- output handling -------------------------------------------------
    def _on_line(self, line: str) -> None:
        self.result.lines.append(line)
        m = _ADD_TOTAL_RE.search(line)
        if m and not self.total_bytes:
            self.total_bytes = int(m.group(1))

    def _on_progress(self, percent: int, count: int | None, name: str | None) -> None:
        fraction = min(max(percent / 100.0, 0.0), 1.0)
        done = int(self.total_bytes * fraction) if self.total_bytes else None
        packed = None
        if self.packed_path:
            now = time.monotonic()
            if now - self._last_packed_check > 0.25:
                self._last_packed_check = now
                packed = _current_size(self.packed_path)
        self.cb.progress(fraction, current=name, done_bytes=done, total_bytes=self.total_bytes, files=count,
                         total_files=self.total_files, packed=packed)

    def _answer(self, proc: subprocess.Popen, text: str) -> None:
        try:
            proc.stdin.write((text + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _handle_prompt(self, proc: subprocess.Popen, term: _Terminal) -> None:
        pending = term.pending
        if _PASSWORD_PROMPT_RE.search(pending):
            term.pending = ""
            self.result.password_prompts += 1
            box = self.password if self.password is not None else PasswordBox()
            verifying = pending.strip().lower().startswith(("verify", "reenter", "re-enter"))
            if verifying and box.value is not None:
                self._answer(proc, box.value)
                return
            # A second prompt for the same run means 7-Zip rejected the first one.
            retry = self.result.password_prompts > 1 and not verifying
            password = box.obtain(self.cb, self.archive_name, retry=retry)
            self._answer(proc, password)
        elif _OVERWRITE_PROMPT_RE.search(pending):
            block = "\n".join(self.result.lines[-12:]) + "\n" + pending
            term.pending = ""
            existing, incoming = _parse_overwrite_prompt(block, self.cwd)
            answer, apply_all = self.cb.ask_overwrite(existing, incoming)
            key = {
                (Answer.REPLACE, False): "y", (Answer.REPLACE, True): "a",
                (Answer.SKIP, False): "n", (Answer.SKIP, True): "s",
                (Answer.KEEP_BOTH, False): "u", (Answer.KEEP_BOTH, True): "u",
            }.get((answer, apply_all), "q")
            self._answer(proc, key)

    # -- main loop --------------------------------------------------------
    def run(self) -> RunResult:
        self.cb.checkpoint()
        if os.environ.get("NAUTILUS_7ZIP_DEBUG"):
            import sys

            print("nautilus-7zip: running", self.argv, "in", self.cwd, file=sys.stderr)
        try:
            proc = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=tool_environment(),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise ArchiveError(_("Could not start 7-Zip ({error}).").format(error=exc.strerror or exc),
                               details=str(exc)) from exc
        self.cb.attach_process(proc)
        out_dec = codecs.getincrementaldecoder("utf-8")("replace")
        err_dec = codecs.getincrementaldecoder("utf-8")("replace")
        term = _Terminal(self._on_line, self._on_progress)
        streams = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
        for fd in streams:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        stderr_parts: list[str] = []
        try:
            open_fds = set(streams)
            while open_fds:
                try:
                    self.cb.checkpoint()
                except Cancelled:
                    _stop(proc)
                    raise
                readable, _w, _x = select.select(list(open_fds), [], [], 0.1)
                for fd in readable:
                    try:
                        data = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        open_fds.discard(fd)
                        continue
                    if streams[fd] == "out":
                        term.feed(out_dec.decode(data))
                    else:
                        stderr_parts.append(err_dec.decode(data))
                if term.pending and (_PASSWORD_PROMPT_RE.search(term.pending)
                                     or _OVERWRITE_PROMPT_RE.search(term.pending)):
                    try:
                        self._handle_prompt(proc, term)
                    except Cancelled:
                        _stop(proc)
                        raise
            term.feed(out_dec.decode(b"", final=True) + "\n")
            stderr_parts.append(err_dec.decode(b"", final=True))
            self.result.returncode = proc.wait()
        finally:
            self.cb.detach_process(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            if proc.poll() is None:
                _stop(proc)
        self.result.stderr = "".join(stderr_parts)
        if self.result.returncode == 255:
            raise Cancelled()
        return self.result


def _stop(proc: subprocess.Popen) -> None:
    """Ask 7-Zip to stop like Ctrl+C would (it then cleans up temp files)."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGCONT)
        os.killpg(pgid, signal.SIGINT)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        proc.wait()


def _current_size(path: str) -> int | None:
    size = None
    for candidate in (path, path + ".tmp", path + ".001"):
        try:
            size = (size or 0) + os.stat(candidate).st_size
        except OSError:
            continue
    return size


def _parse_overwrite_prompt(block: str, cwd: str | None) -> tuple[FileDescription, FileDescription]:
    sides: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("Would you like to replace the existing file") or stripped.startswith(
                "with the file from archive"):
            current = {}
            sides.append(current)
        elif current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip().lower()] = value.strip()

    def describe(d: dict[str, str] | None, existing: bool) -> FileDescription:
        d = d or {}
        path = d.get("path", "")
        if existing and path and cwd and not os.path.isabs(path):
            path = os.path.join(cwd, path)
        if existing and path:
            return FileDescription.from_path(path)
        size = None
        m = re.match(r"(\d+)", d.get("size", ""))
        if m:
            size = int(m.group(1))
        return FileDescription(path, size, _parse_time(d.get("modified")))

    return describe(sides[0] if sides else None, True), describe(sides[1] if len(sides) > 1 else None, False)


def _parse_time(text: str | None) -> float | None:
    if not text:
        return None
    text = text.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", text)
    if not m:
        return None
    try:
        value = _dt.datetime(*(int(m.group(i)) for i in range(1, 7)))
    except ValueError:
        return None
    fraction = float("0." + m.group(7)) if m.group(7) else 0.0
    return value.timestamp() + fraction


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    text = text.strip()
    return int(text) if text.isdigit() else None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def parse_listing(text: str, archive: str) -> ArchiveInfo:
    info = ArchiveInfo(path=archive, engine="7-Zip")
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n and lines[i].strip() != "--":
        i += 1
    props: dict[str, str] = {}
    i += 1
    while i < n and not lines[i].startswith("----------"):
        line = lines[i]
        if " = " in line:
            key, value = line.split(" = ", 1)
            props[key.strip()] = value
        elif line.strip() not in ("", "--", "----") and "warning" in line.lower():
            info.warnings.append(line.strip())
        i += 1
    i += 1
    record: dict[str, str] = {}

    def flush() -> None:
        if "Path" not in record:
            record.clear()
            return
        info.entries.append(_entry_from_record(record))
        record.clear()

    while i < n:
        line = lines[i]
        if not line.strip():
            flush()
        elif " = " in line:
            key, value = line.split(" = ", 1)
            record[key.strip()] = value
        elif line.endswith(" =") and "=" in line:
            record[line[:-2].strip()] = ""
        i += 1
    flush()

    info.type = props.get("Type", "")
    info.physical_size = _int(props.get("Total Physical Size")) or _int(props.get("Physical Size"))
    info.headers_size = _int(props.get("Headers Size"))
    info.method = props.get("Method") or None
    if props.get("Solid") in ("+", "-"):
        info.solid = props.get("Solid") == "+"
    info.blocks = _int(props.get("Blocks"))
    info.volumes = _int(props.get("Volumes"))
    info.comment = props.get("Comment") or None
    info.properties = {k: v for k, v in props.items() if k not in ("Path",)}
    return info


def _entry_from_record(r: dict[str, str]) -> Entry:
    path = r.get("Path", "").replace("\\", "/").strip("/")
    attributes = r.get("Attributes", "")
    is_dir = r.get("Folder") == "+" or attributes.startswith("D")
    mode = None
    m = re.search(r"(?:^|\s)([-dlcbps])([-r][-w][-xsStT])([-r][-w][-xsStT])([-r][-w][-xsStT])", attributes)
    link = r.get("Symbolic Link") or None
    if m:
        mode = _mode_from_string(m.group(0).strip())
        if m.group(1) == "l" and link is None:
            link = ""
    return Entry(
        path=path,
        is_dir=is_dir,
        size=_int(r.get("Size")),
        packed_size=_int(r.get("Packed Size")),
        mtime=_parse_time(r.get("Modified")),
        encrypted=r.get("Encrypted") == "+",
        crc=r.get("CRC") or None,
        method=r.get("Method") or None,
        link_target=link,
        mode=mode,
        block=_int(r.get("Block")),
        comment=r.get("Comment") or None,
    )


def _mode_from_string(text: str) -> int:
    """"-rwxr-xr-x" → 0o755 (setuid/sticky letters count as executable)."""
    mode = 0
    bits = (0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001)
    for index, bit in enumerate(bits, start=1):
        if index >= len(text):
            break
        char = text[index]
        if char in "rwxsStT" and char not in "ST":
            mode |= bit
    return mode


# ---------------------------------------------------------------------------
# Error analysis
# ---------------------------------------------------------------------------


def analyse_failure(result: RunResult, tool: ToolInfo, archive: str) -> ArchiveError:
    text = result.text
    lower = text.lower()
    name = os.path.basename(archive)
    details = "\n".join(result.error_lines()) or "\n".join(line for line in result.lines if line.strip())[-4000:]
    _stem, fmt = formats.split_extension(name)

    def err(message: str, kind: ErrorKind) -> ArchiveError:
        return ArchiveError(message, kind=kind, details=details, archive=archive)

    if "wrong password" in lower or "cannot open encrypted archive" in lower or \
            "can not open encrypted archive" in lower:
        return err(_("The password is incorrect."), ErrorKind.WRONG_PASSWORD)
    m = re.search(r"Missing volume\s*:\s*(.+)", text)
    if m:
        volume = os.path.basename(m.group(1).strip())
        return err(_("A part of this multi-volume archive is missing: “{volume}”.").format(volume=volume),
                   ErrorKind.MISSING_VOLUME)
    if "no space left on device" in lower or "not enough space on the disk" in lower:
        return err(_("There is not enough space on the disk."), ErrorKind.DISK_FULL)
    if "permission denied" in lower or "access is denied" in lower:
        return err(_("You don’t have permission to access this location."), ErrorKind.PERMISSION)
    if "unsupported method" in lower or "unsupported feature" in lower:
        if fmt is not None and fmt.id in ("rar", "cbr") and not tool.has_rar:
            return err(_("RAR support is not installed for 7-Zip."), ErrorKind.NEEDS_RAR)
        return err(_("“{name}” uses a compression method that this 7-Zip build does not support.").format(
            name=name), ErrorKind.UNSUPPORTED)
    if "cannot open the file as archive" in lower or "can not open the file as archive" in lower or \
            "can't open as archive" in lower or "is not archive" in lower:
        if fmt is not None and fmt.id in ("rar", "cbr") and not tool.has_rar:
            return err(_("RAR support is not installed for 7-Zip."), ErrorKind.NEEDS_RAR)
        return err(_("“{name}” is not a supported archive, or it is damaged.").format(name=name),
                   ErrorKind.NOT_ARCHIVE)
    if "unexpected end of archive" in lower or "unexpected end of data" in lower or "truncated" in lower:
        return err(_("The archive is incomplete. It may not have finished downloading."), ErrorKind.TRUNCATED)
    if "data error" in lower or "crc failed" in lower or "headers error" in lower or "is damaged" in lower:
        return err(_("The archive is damaged."), ErrorKind.DAMAGED)
    if "not enough memory" in lower or "can't allocate required memory" in lower or result.returncode == 8:
        return err(_("There is not enough memory to process this archive."), ErrorKind.MEMORY)
    if "the system cannot find the file" in lower or "no such file or directory" in lower or \
            "cannot find archive" in lower:
        return err(_("“{name}” could not be found.").format(name=name), ErrorKind.NOT_FOUND)
    if result.returncode == 7:
        return err(_("7-Zip rejected the command line."), ErrorKind.GENERIC)
    first = result.error_lines()[0] if result.error_lines() else _("7-Zip reported an error.")
    first = re.sub(r"^ERROR:\s*", "", first)
    return err(first, ErrorKind.GENERIC)


def _summary(result: RunResult) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in result.lines:
        m = re.match(r"^(Folders|Files|Size|Compressed):\s+(\d+)\s*$", line.strip())
        if m:
            out[m.group(1).lower()] = int(m.group(2))
    return out


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class SevenZipEngine:
    name = "7-Zip"

    def __init__(self, tool: ToolInfo) -> None:
        self.tool = tool

    def _run(self, args, cb, **kwargs) -> RunResult:
        return SevenZipRunner(self.tool, args, cb, **kwargs).run()

    # -- reading ---------------------------------------------------------
    def list(self, archive: str, cb: Callbacks, password: PasswordBox) -> ArchiveInfo:
        name = os.path.basename(archive)
        retry = False
        while True:
            cb.checkpoint()
            if retry:
                password.obtain(cb, name, retry=True)
            result = self._run(["l", "-slt", *COMMON, "--", archive], cb, password=password, archive_name=name)
            if result.returncode in (0, 1) and "--" in result.lines:
                info = parse_listing("\n".join(result.lines), archive)
                info.headers_encrypted = result.password_prompts > 0
                if info.headers_encrypted:
                    password.validated = True
                for line in result.error_lines():
                    if "warning" in line.lower():
                        info.warnings.append(line)
                return info
            error = analyse_failure(result, self.tool, archive)
            if error.kind is ErrorKind.WRONG_PASSWORD:
                retry = True
                continue
            raise error

    def verify_password(self, archive: str, info: ArchiveInfo, cb: Callbacks, password: PasswordBox) -> None:
        """Make sure we hold the right password before a long extraction."""
        encrypted = [e for e in info.entries if e.encrypted and not e.is_dir]
        if not encrypted or password.validated:
            return
        # The cheapest file to test: first file of the lowest solid block,
        # otherwise the smallest one.
        if any(e.block is not None for e in encrypted):
            probe = min(encrypted, key=lambda e: (e.block if e.block is not None else 1 << 62,
                                                  encrypted.index(e)))
        else:
            probe = min(encrypted, key=lambda e: e.size or 0)
        name = os.path.basename(archive)
        retry = False
        while True:
            password.obtain(cb, name, retry=retry)
            cb.stage(_("Checking password…"))
            result = self._run(["t", *COMMON, "-spd", "--", archive, probe.path], cb, password=password,
                               archive_name=name)
            lower = result.text.lower()
            if result.returncode == 0 and "wrong password" not in lower:
                password.validated = True
                return
            error = analyse_failure(result, self.tool, archive)
            if error.kind is ErrorKind.WRONG_PASSWORD or "data error" in lower or "crc failed" in lower:
                retry = True
                continue
            raise error

    def extract(self, archive: str, staging: str, cb: Callbacks, password: PasswordBox, *,
                members: list[str] | None = None, flatten: bool = False, total_bytes: int | None = None,
                total_files: int | None = None) -> Report:
        command = "e" if flatten else "x"
        args = [command, *COMMON, f"-o{staging}", "-spd"]
        if flatten:
            args.append("-aou")
        args += ["--", archive, *(members or [])]
        result = self._run(args, cb, password=password, archive_name=os.path.basename(archive),
                           total_bytes=total_bytes, total_files=total_files, cwd=staging)
        return self._report(result, archive)

    def test(self, archive: str, cb: Callbacks, password: PasswordBox, *, total_bytes: int | None = None,
             total_files: int | None = None) -> Report:
        result = self._run(["t", *COMMON, "--", archive], cb, password=password,
                           archive_name=os.path.basename(archive), total_bytes=total_bytes,
                           total_files=total_files)
        return self._report(result, archive)

    def _report(self, result: RunResult, archive: str) -> Report:
        report = Report()
        summary = _summary(result)
        report.files = summary.get("files", 0)
        report.folders = summary.get("folders", 0)
        report.size = summary.get("size", 0)
        report.packed = summary.get("compressed")
        if result.returncode == 0:
            for line in result.error_lines():
                if line.startswith("WARNING"):
                    report.warnings.append(line)
            return report
        if result.returncode == 1:  # warnings only
            report.warnings.extend(result.error_lines() or [_("7-Zip reported warnings.")])
            return report
        error = analyse_failure(result, self.tool, archive)
        report.errors.append(error.message)
        report.error_kind = error.kind
        details = [line for line in result.error_lines() if line != error.message]
        report.warnings.extend(details[:200])
        return report

    # -- writing -----------------------------------------------------------
    def create(self, archive: str, cwd: str, names: list[str], switches: list[str], cb: Callbacks,
               password: PasswordBox | None = None, total_bytes: int | None = None) -> Report:
        args = ["a", *COMMON, *switches]
        if password is not None and password.value:
            args.append("-p")
        args += ["--", archive, *names]
        result = self._run(args, cb, password=password, cwd=cwd, archive_name=os.path.basename(archive),
                           total_bytes=total_bytes, packed_path=archive)
        report = self._report(result, archive)
        try:
            report.packed = os.stat(archive).st_size
        except OSError:
            pass
        return report

    # -- checksums -------------------------------------------------------
    def hash(self, cwd: str, names: list[str], algorithms: list[hashalgos.HashAlgorithm], cb: Callbacks,
             total_bytes: int | None = None) -> HashTable:
        args = ["h", *[f"-scrc{a.sevenzip}" for a in algorithms], *COMMON, "-spd", "--", *names]
        result = self._run(args, cb, cwd=cwd, total_bytes=total_bytes)
        if result.returncode not in (0, 1):
            raise analyse_failure(result, self.tool, names[0] if names else cwd)
        table = parse_hash_output(result.lines, algorithms)
        table.warnings.extend(line for line in result.error_lines())
        return table


@dataclass
class HashRow:
    name: str
    size: int | None
    digests: dict[str, str]
    is_dir: bool = False


@dataclass
class HashTable:
    rows: list[HashRow] = field(default_factory=list)
    data_sums: dict[str, str] = field(default_factory=dict)
    names_sums: dict[str, str] = field(default_factory=dict)
    total_size: int | None = None
    files: int | None = None
    folders: int | None = None
    warnings: list[str] = field(default_factory=list)


def parse_hash_output(lines: list[str], algorithms: list[hashalgos.HashAlgorithm]) -> HashTable:
    table = HashTable()
    spans: list[tuple[int, int]] = []
    header: list[str] = []
    state = "start"
    for index, line in enumerate(lines):
        if state == "start" and re.match(r"^-+(\s+-+)+\s*$", line) and index > 0:
            header_line = lines[index - 1]
            spans = [(m.start(), m.end()) for m in re.finditer(r"-+", line)]
            header = [header_line[s:e + 1].strip() if s < len(header_line) else "" for s, e in spans]
            state = "rows"
            continue
        if state == "rows":
            if re.match(r"^-+(\s+-+)+\s*$", line):
                state = "totals"
                continue
            if not line.strip():
                continue
            cells = [line[s:e].strip() for s, e in spans[:-1]]
            name = line[spans[-1][0]:].strip() if spans else line.strip()
            digests = {}
            size = None
            for column, cell in zip(header, cells, strict=False):
                upper = column.upper()
                if upper == "SIZE":
                    size = int(cell) if cell.isdigit() else None
                elif upper in hashalgos.BY_SEVENZIP and cell:
                    algo = hashalgos.BY_SEVENZIP[upper]
                    digests[algo.id] = hashalgos.format_digest(algo.id, cell)
            is_dir = name.endswith("/") or not digests
            table.rows.append(HashRow(name.rstrip("/"), size, digests, is_dir))
            continue
        m = re.match(r"^(\S+)\s+for data( and names)?:\s+(\S+)", line.strip())
        if m:
            label = m.group(1).upper()
            algo = hashalgos.BY_SEVENZIP.get(label)
            if algo is not None:
                value = hashalgos.format_digest(algo.id, m.group(3))
                (table.names_sums if m.group(2) else table.data_sums)[algo.id] = value
            continue
        m = re.match(r"^(Folders|Files|Size):\s+(\d+)\s*$", line.strip())
        if m:
            value = int(m.group(2))
            if m.group(1) == "Folders":
                table.folders = value
            elif m.group(1) == "Files":
                table.files = value
            else:
                table.total_size = value
    return table
