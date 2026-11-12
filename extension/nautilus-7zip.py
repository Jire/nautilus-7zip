# SPDX-License-Identifier: MIT
"""nautilus-7zip — the full 7-Zip right-click menu for GNOME Files.

This file is loaded by nautilus-python inside the Files process.  It only
builds the menu (from the pure-Python ``nautilus7zip`` package) and launches
the helper application, which does the actual work in its own process so the
file manager never blocks or crashes because of an archive.
"""

import os
import sys
import time

import gi

for _version in ("4.1", "4.0"):
    try:
        gi.require_version("Nautilus", _version)
        break
    except ValueError:
        continue

from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

# Replaced by the installer; the fallbacks below cover running from a checkout.
PKGDATADIR = "@PKGDATADIR@"
HELPER = "@HELPER@"
APP_ID = "io.github.jire.Nautilus7Zip"


def _find_package_dir():
    here = os.path.dirname(os.path.realpath(__file__))
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    for candidate in (
        PKGDATADIR,
        os.path.join(here, "..", "src"),
        os.path.join(data_home, "nautilus-7zip"),
        "/usr/local/share/nautilus-7zip",
        "/usr/share/nautilus-7zip",
    ):
        if candidate.startswith("@"):
            continue
        if os.path.isfile(os.path.join(candidate, "nautilus7zip", "menu.py")):
            return os.path.abspath(candidate)
    return None


_PACKAGE_DIR = _find_package_dir()
if _PACKAGE_DIR and _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

try:
    from nautilus7zip import menu as _menu
    from nautilus7zip import settings as _settings
    from nautilus7zip import tool as _tool
except ImportError as _error:  # pragma: no cover - reported once in the journal
    print(f"nautilus-7zip: cannot load the nautilus7zip package ({_error})", file=sys.stderr)
    _menu = None


_DEBUG = bool(os.environ.get("NAUTILUS_7ZIP_DEBUG"))


def _debug(*parts):
    if _DEBUG:
        print("nautilus-7zip:", *parts, file=sys.stderr, flush=True)


_debug("loaded from", __file__, "package", _PACKAGE_DIR)


def _helper_command():
    if not HELPER.startswith("@") and os.access(HELPER, os.X_OK):
        return [HELPER]
    if _PACKAGE_DIR:
        launcher = os.path.join(_PACKAGE_DIR, "nautilus-7zip")
        if os.access(launcher, os.X_OK):
            return [launcher]
        return ["/usr/bin/env", "python3", "-m", "nautilus7zip"]
    return None


class SevenZipMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self):
        super().__init__()
        self._settings = _settings.Settings() if _menu else None
        self._names_cache = (0.0, None, set())

    # -- helpers ------------------------------------------------------------
    def _sevenzip_names(self):
        override = self._settings.get("sevenzip_path") or None
        stamp, cached_override, names = self._names_cache
        now = time.monotonic()
        if now - stamp > 5.0 or cached_override != override:
            names = {os.path.basename(p) for p in _tool.candidates(override)}
            self._names_cache = (now, override, names)
        return names

    @staticmethod
    def _local_path(info):
        location = None
        try:
            uri = info.get_activation_uri() or info.get_uri()
            location = Gio.File.new_for_uri(uri) if uri else info.get_location()
        except Exception:
            location = info.get_location()
        if location is None:
            return None
        return location.get_path()

    @staticmethod
    def _is_a(mime, parent):
        return Gio.content_type_is_a(mime, parent)

    def _activation_token(self):
        """Ask Files for an XDG activation token so our windows get focus."""
        try:
            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            if display is None:
                return None
            context = display.get_app_launch_context()
            try:
                app_info = Gio.DesktopAppInfo.new(APP_ID + ".desktop")
            except TypeError:
                app_info = None
            if app_info is None:  # running from a checkout: no .desktop file installed
                return None
            return context.get_startup_notify_id(app_info, [])
        except Exception:
            return None

    def _launch(self, _item, args):
        command = _helper_command()
        if command is None:
            print("nautilus-7zip: helper application not found", file=sys.stderr)
            return
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
        token = self._activation_token()
        if token:
            launcher.setenv("XDG_ACTIVATION_TOKEN", token, True)
            launcher.setenv("DESKTOP_STARTUP_ID", token, True)
        if command[0] == "/usr/bin/env" and _PACKAGE_DIR:
            existing = os.environ.get("PYTHONPATH")
            launcher.setenv("PYTHONPATH", _PACKAGE_DIR + (os.pathsep + existing if existing else ""), True)
        try:
            launcher.spawnv(command + list(args))
        except GLib.Error as error:
            print(f"nautilus-7zip: could not start the helper: {error.message}", file=sys.stderr)

    def _convert(self, node, prefix="SevenZip::"):
        item = Nautilus.MenuItem(name=prefix + node.id, label=node.label, tip=node.tip, sensitive=node.sensitive)
        if node.children:
            submenu = Nautilus.Menu()
            for child in node.children:
                submenu.append_item(self._convert(child, prefix + node.id + "::"))
            item.set_submenu(submenu)
        elif node.action:
            item.connect("activate", self._launch, list(node.action))
        return item

    # -- Nautilus.MenuProvider -------------------------------------------
    def get_file_items(self, *args):
        # Nautilus 43+ passes (files); older versions passed (window, files).
        files = args[-1]
        if _menu is None or not files:
            return []
        try:
            selection = []
            for info in files:
                path = self._local_path(info)
                if path is None:
                    _debug("not a local file:", info.get_uri())
                    return []
                selection.append(_menu.Selected(path, info.get_mime_type(), info.is_directory()))
            nodes = _menu.build(selection, self._settings, sevenzip_names=self._sevenzip_names(), is_a=self._is_a)
            _debug("menu for", [s.path for s in selection], "->", [n.label for n in nodes])
            return [self._convert(node) for node in nodes]
        except Exception as error:
            import traceback

            print(f"nautilus-7zip: {error}", file=sys.stderr)
            traceback.print_exc()
            return []

    def get_background_items(self, *args):
        return []
