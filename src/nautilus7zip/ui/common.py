# SPDX-License-Identifier: MIT
"""Shared UI helpers: formatting, the thread bridge, file manager integration."""

from __future__ import annotations

import math
import os
import signal
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..engines.base import Callbacks, Cancelled  # noqa: E402
from ..i18n import _  # noqa: E402

ADW_VERSION = (Adw.get_major_version(), Adw.get_minor_version())


def adw_at_least(minor: int) -> bool:
    return (1, minor) <= ADW_VERSION


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_size(value: int | None) -> str:
    if value is None:
        return "—"
    return GLib.format_size(value)


def format_duration(seconds: float | None) -> str:
    if seconds is None or math.isnan(seconds) or seconds < 0:
        return "—"
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_time(timestamp: float | None) -> str:
    """Nautilus-like dates: "Today 14:02", "Yesterday 09:13", "3 Mar 2024"."""
    if timestamp is None:
        return "—"
    try:
        dt = GLib.DateTime.new_from_unix_local(int(timestamp))
    except (TypeError, OverflowError, ValueError):
        return "—"
    if dt is None:
        return "—"
    now = GLib.DateTime.new_now_local()
    days = now.get_day_of_year() - dt.get_day_of_year()
    if now.get_year() == dt.get_year() and days == 0:
        return _("Today {time}").format(time=dt.format("%H:%M"))
    if now.get_year() == dt.get_year() and days == 1:
        return _("Yesterday {time}").format(time=dt.format("%H:%M"))
    if now.get_year() == dt.get_year():
        return dt.format("%-d %b %H:%M")
    return dt.format("%-d %b %Y")


def format_ratio(packed: int | None, size: int | None) -> str:
    if not packed or not size:
        return "—"
    return f"{packed * 100 / size:.0f}%"


def content_icon(path_or_name: str, *, is_dir: bool = False, symbolic: bool = False) -> Gio.Icon:
    if is_dir:
        return Gio.ThemedIcon.new("folder-symbolic" if symbolic else "folder")
    content_type, _uncertain = Gio.content_type_guess(os.path.basename(path_or_name), None)
    if symbolic:
        return Gio.content_type_get_symbolic_icon(content_type)
    return Gio.content_type_get_icon(content_type)


def display_folder(path: str) -> str:
    """Show folders the way Files does: "Downloads", "Home", "~/Projects/x"."""
    home = os.path.expanduser("~")
    path = os.path.abspath(path)
    if path == home:
        return _("Home")
    if path.startswith(home + os.sep):
        relative = path[len(home) + 1:]
        return relative if os.sep not in relative else "~/" + relative
    return path


# ---------------------------------------------------------------------------
# File manager integration
# ---------------------------------------------------------------------------


def show_in_files(paths: list[str], window: Gtk.Window | None = None) -> None:
    """Open the folder and select the items (org.freedesktop.FileManager1)."""
    paths = [p for p in paths if p]
    if not paths:
        return
    uris = [Gio.File.new_for_path(p).get_uri() for p in paths]
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call(
            "org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            "ShowItems",
            GLib.Variant("(ass)", (uris, "")),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
    except GLib.Error:
        open_folder(os.path.dirname(paths[0]), window)


def open_folder(path: str, window: Gtk.Window | None = None) -> None:
    launch_file(path, window)


def launch_file(path: str, window: Gtk.Window | None = None) -> None:
    file = Gio.File.new_for_path(path)
    try:
        launcher = Gtk.FileLauncher.new(file)
        launcher.launch(window, None, None)
    except (AttributeError, TypeError):
        Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)


def copy_to_clipboard(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set(text)


# ---------------------------------------------------------------------------
# Thread bridge
# ---------------------------------------------------------------------------


class GtkCallbacks(Callbacks):
    """Callbacks used by worker threads; everything GTK happens on the main loop."""

    def __init__(self, host) -> None:
        super().__init__()
        self.host = host  # object with prompt_password / prompt_overwrite
        self._lock = threading.Lock()
        self._state: dict = {}
        self._paused = threading.Event()
        self._processes: set = set()
        self.started = time.monotonic()
        self._pause_started: float | None = None
        self._asking_since: float | None = None
        self.idle_time = 0.0  # paused or waiting for the user

    # -- progress -------------------------------------------------------------
    def stage(self, text: str) -> None:
        with self._lock:
            self._state["stage"] = text
            self._state["fraction"] = None
            self._state["current"] = None

    def progress(self, fraction, *, current=None, done_bytes=None, total_bytes=None, files=None,
                 total_files=None, packed=None) -> None:
        with self._lock:
            self._state["fraction"] = fraction
            for key, value in (("current", current), ("done_bytes", done_bytes), ("total_bytes", total_bytes),
                               ("files", files), ("total_files", total_files), ("packed", packed)):
                if value is not None:
                    self._state[key] = value
            self._state["updated"] = True

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
            self._state["updated"] = False
            return state

    def active_seconds(self) -> float:
        now = time.monotonic()
        idle = self.idle_time
        if self._pause_started is not None:
            idle += now - self._pause_started
        if self._asking_since is not None:
            idle += now - self._asking_since
        return max(now - self.started - idle, 0.0)

    @property
    def asking(self) -> bool:
        return self._asking_since is not None

    # -- control --------------------------------------------------------------
    def checkpoint(self) -> None:
        if self.cancelled:
            raise Cancelled()
        while self._paused.is_set():
            if self.cancelled:
                raise Cancelled()
            time.sleep(0.05)

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def _signal(self, sig) -> None:
        for proc in list(self._processes):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (OSError, ProcessLookupError):
                pass

    def pause(self) -> None:
        if not self._paused.is_set():
            self._paused.set()
            self._pause_started = time.monotonic()
            self._signal(signal.SIGSTOP)

    def resume(self) -> None:
        if self._paused.is_set():
            self._signal(signal.SIGCONT)
            if self._pause_started is not None:
                self.idle_time += time.monotonic() - self._pause_started
                self._pause_started = None
            self._paused.clear()

    def cancel(self) -> None:
        self.cancelled = True
        self._signal(signal.SIGCONT)
        self._paused.clear()

    def lower_priority(self) -> None:
        """Background mode: be nice to the rest of the system."""
        for proc in list(self._processes):
            try:
                os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
            except OSError:
                pass
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (OSError, AttributeError):
            pass

    def attach_process(self, process) -> None:
        self._processes.add(process)
        if self._paused.is_set():
            self._signal(signal.SIGSTOP)

    def detach_process(self, process) -> None:
        self._processes.discard(process)

    # -- questions --------------------------------------------------------------
    def _ask(self, method, *args):
        event = threading.Event()
        box: dict = {}

        def done(value) -> None:
            box["value"] = value
            event.set()

        def show() -> bool:
            try:
                method(*args, done)
            except Exception:  # never leave the worker waiting forever
                import traceback

                traceback.print_exc()
                done(None)
            return False

        GLib.idle_add(show)
        self._asking_since = time.monotonic()
        try:
            while not event.wait(0.1):
                if self.cancelled:
                    raise Cancelled()
        finally:
            self.idle_time += time.monotonic() - self._asking_since
            self._asking_since = None
        return box.get("value")

    def ask_password(self, archive_name: str, retry: bool) -> str | None:
        return self._ask(self.host.prompt_password, archive_name, retry)

    def ask_overwrite(self, existing, incoming):
        from ..engines.base import Answer

        answer = self._ask(self.host.prompt_overwrite, existing, incoming)
        return answer if answer is not None else (Answer.CANCEL, False)


def run_in_thread(target, on_done) -> threading.Thread:
    """Run ``target()`` in a daemon thread; ``on_done(result, error)`` runs on the main loop."""

    def worker() -> None:
        result = error = None
        try:
            result = target()
        except BaseException as exc:
            error = exc
        GLib.idle_add(lambda: (on_done(result, error), False)[1])

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def clamp_box(child: Gtk.Widget, maximum: int = 560) -> Adw.Clamp:
    clamp = Adw.Clamp(maximum_size=maximum, tightening_threshold=maximum - 120)
    clamp.set_child(child)
    return clamp


def close_on_escape(window: Gtk.Window) -> None:
    """Dialog-like windows close with Esc, as GNOME users expect."""
    controller = Gtk.ShortcutController()
    controller.set_scope(Gtk.ShortcutScope.LOCAL)
    controller.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                             Gtk.CallbackAction.new(lambda *_a: window.close() or True)))
    window.add_controller(controller)


def make_spinner() -> Gtk.Widget:
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner()
    spinner = Gtk.Spinner()
    spinner.start()
    return spinner
