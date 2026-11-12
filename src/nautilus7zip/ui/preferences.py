# SPDX-License-Identifier: MIT
"""The main window: setup status and preferences."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time

from gi.repository import Adw, Gio, GLib, Gtk

from .. import APP_ID, tool
from ..i18n import _
from .common import copy_to_clipboard, run_in_thread

MENU_ITEM_LABELS = (
    ("open", _("Open Archive")),
    ("extract", _("Extract Files…")),
    ("extract_here", _("Extract Here")),
    ("extract_to", _("Extract to “name/”")),
    ("test", _("Test Archive")),
    ("verify", _("Verify Checksums")),
    ("add", _("Add to Archive…")),
    ("add_email", _("Compress and Email…")),
    ("add_7z", _("Add to “name.7z”")),
    ("add_7z_email", _("Compress to “name.7z” and Email")),
    ("add_zip", _("Add to “name.zip”")),
    ("add_zip_email", _("Compress to “name.zip” and Email")),
    ("hash", _("CRC SHA")),
)

EXTENSION_DIRS = (
    "~/.local/share/nautilus-python/extensions",
    "/usr/local/share/nautilus-python/extensions",
    "/usr/share/nautilus-python/extensions",
)


def nautilus_python_installed() -> bool:
    patterns = ("/usr/lib*/nautilus/extensions-4/libnautilus-python.so",
                "/usr/lib/*/nautilus/extensions-4/libnautilus-python.so",
                "/usr/local/lib*/nautilus/extensions-4/libnautilus-python.so")
    return any(glob.glob(p) for p in patterns)


def installed_extension() -> str | None:
    data_home = os.environ.get("XDG_DATA_HOME")
    dirs = ([os.path.join(data_home, "nautilus-python/extensions")] if data_home else []) + \
        [os.path.expanduser(d) for d in EXTENSION_DIRS]
    for directory in dirs:
        path = os.path.join(directory, "nautilus-7zip.py")
        if os.path.exists(path):
            return path
    return None


def _process_start(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        with open("/proc/uptime", encoding="ascii") as fh:
            uptime = float(fh.read().split()[0])
        started_ticks = int(fields[19])
        return time.time() - uptime + started_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def files_needs_restart() -> bool:
    """True if a running Files process predates the installed extension."""
    extension = installed_extension()
    if extension is None:
        return False
    try:
        installed_at = os.stat(extension).st_mtime
    except OSError:
        return False
    uid = os.getuid()
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8") as fh:
                if fh.read().strip() != "nautilus":
                    continue
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            # Only the Files instance of this login session matters.
            with open(f"/proc/{entry}/environ", "rb") as fh:
                environ = dict(item.split(b"=", 1) for item in fh.read().split(b"\0") if b"=" in item)
            other = environ.get(b"DBUS_SESSION_BUS_ADDRESS", b"").decode("utf-8", "replace")
            if bus and other and other != bus:
                continue
        except OSError:
            continue
        started = _process_start(int(entry))
        if started is not None and started < installed_at:
            return True
        try:
            with open(f"/proc/{entry}/maps", encoding="utf-8", errors="replace") as fh:
                if "libnautilus-python" not in fh.read():
                    return True
        except OSError:
            pass
    return False


class PreferencesWindow(Adw.ApplicationWindow):
    def __init__(self, app) -> None:
        super().__init__(application=app, title=_("7-Zip for Files"))
        self.app = app
        self.settings = app.settings
        self.set_default_size(640, 760)
        self.set_size_request(360, 400)

        header = Adw.HeaderBar()
        open_button = Gtk.Button(tooltip_text=_("Open an archive"))
        content = Adw.ButtonContent(icon_name="document-open-symbolic", label=_("_Open Archive…"),
                                    use_underline=True)
        open_button.set_child(content)
        open_button.connect("clicked", lambda _b: self._open_archive())
        header.pack_start(open_button)
        menu = Gio.Menu()
        menu.append(_("_Reset All Settings…"), "win.reset")
        menu.append(_("_About 7-Zip for Files"), "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text=_("Main menu")))
        reset = Gio.SimpleAction.new("reset", None)
        reset.connect("activate", lambda *_a: self._confirm_reset())
        self.add_action(reset)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)
        self._build()

    # -----------------------------------------------------------------------
    def _build(self) -> None:
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        # Status ----------------------------------------------------------------
        status = Adw.PreferencesGroup(title=_("Status"))
        self.tool_row = Adw.ActionRow(title=_("Checking 7-Zip…"))
        self.tool_icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
        self.tool_row.add_prefix(self.tool_icon)
        status.add(self.tool_row)
        self.rar_row = Adw.ActionRow(visible=False)
        status.add(self.rar_row)
        self.files_row = Adw.ActionRow()
        status.add(self.files_row)
        page.add(status)
        run_in_thread(lambda: (tool.default_tool(self.settings.get("sevenzip_path") or None),
                               nautilus_python_installed(), installed_extension(), files_needs_restart()),
                      self._on_status)

        # Context menu --------------------------------------------------------------
        menu_group = Adw.PreferencesGroup(title=_("Context Menu"),
                                          description=_("Changes apply the next time you right-click."))
        menu_group.add(self._switch(_("Group items in a “7-Zip” submenu"), "cascaded",
                                    _("Otherwise they appear directly in the right-click menu")))
        items = Adw.ExpanderRow(title=_("Menu items"), subtitle=_("Choose which commands are shown"))
        for key, label in MENU_ITEM_LABELS:
            items.add_row(self._switch(label, f"menu_items.{key}"))
        menu_group.add(items)
        kinds = Adw.ExpanderRow(title=_("Also treat as archives"),
                                subtitle=_("Offer extraction for files that are archives inside"))
        kinds.add_row(self._switch(_("Disk images"), "categories.disk", _(".iso, .img, .dmg, .vhd, .qcow2…")))
        kinds.add_row(self._switch(_("Packages and installers"), "categories.package",
                                   _(".deb, .rpm, .jar, .apk, .msi, .exe…")))
        kinds.add_row(self._switch(_("Documents"), "categories.document", _(".docx, .xlsx, .odt, .epub…")))
        menu_group.add(kinds)
        page.add(menu_group)

        # Extraction ----------------------------------------------------------------
        extraction = Adw.PreferencesGroup(title=_("Extraction"))
        extraction.add(self._switch(_("Eliminate duplication of root folder"), "eliminate_root_duplication",
                                    _("“Extract to” avoids “name/name/…” when the archive already contains a "
                                      "“name” folder")))
        extraction.add(self._switch(_("Extract Here creates a folder when needed"), "smart_extract_here",
                                    _("If an archive has several items at its top level, they go into a new "
                                      "folder instead of spilling into the current one")))
        choices = [("builtin", _("Built-in browser"))]
        if shutil.which("7zFM"):
            choices.append(("7zfm", _("7-Zip File Manager")))
        choices.append(("default", _("Default application")))
        combo = Adw.ComboRow(title=_("Open archives with"), model=Gtk.StringList.new([c[1] for c in choices]),
                             subtitle=_("Used by “Open Archive”"))
        current = self.settings.get("open_with", "builtin")
        combo.set_selected(next((i for i, c in enumerate(choices) if c[0] == current), 0))
        combo.connect("notify::selected", lambda r, _p: self.settings.set("open_with", choices[r.get_selected()][0]))
        extraction.add(combo)
        page.add(extraction)

        # Behaviour -------------------------------------------------------------
        behaviour = Adw.PreferencesGroup(title=_("Notifications"))
        behaviour.add(self._switch(_("Notify when long operations finish"), "notify_when_done",
                                   _("When the window is in the background or was sent to the background")))
        page.add(behaviour)

        advanced = Adw.PreferencesGroup(title=_("Advanced"))
        self.path_row = Adw.EntryRow(title=_("7-Zip program (leave empty to detect automatically)"),
                                     text=self.settings.get("sevenzip_path", ""), show_apply_button=True)
        self.path_row.connect("apply", self._on_path_apply)
        advanced.add(self.path_row)
        page.add(advanced)

    def _switch(self, title: str, key: str, subtitle: str | None = None) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, active=bool(self.settings.get(key, True)))
        if subtitle:
            row.set_subtitle(subtitle)
        row.connect("notify::active", lambda r, _p: self.settings.set(key, r.get_active()))
        return row

    def _on_status(self, result, error) -> None:
        if error is not None or result is None:
            return
        info, has_python, extension, needs_restart = result
        if info is not None:
            self.tool_icon.set_from_icon_name("object-select-symbolic")
            self.tool_icon.add_css_class("success")
            self.tool_row.set_title(info.version_text)
            self.tool_row.set_subtitle(_("{path} · {count} formats").format(path=info.path,
                                                                          count=len(info.read_formats)))
            family = tool.distro_family()
            if not info.has_rar:
                self.rar_row.set_visible(True)
                self.rar_row.set_title(_("RAR archives are not supported by this 7-Zip build"))
                command = tool.install_command(rar=True) if family == "debian" else None
                self.rar_row.set_subtitle(command or _("Install a 7-Zip build with RAR support"))
                icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                icon.add_css_class("warning")
                self.rar_row.add_prefix(icon)
                if command:
                    self.rar_row.add_suffix(self._copy_button(command))
        else:
            self.tool_icon.set_from_icon_name("dialog-warning-symbolic")
            self.tool_icon.add_css_class("warning")
            self.tool_row.set_title(_("7-Zip is not installed"))
            command = tool.install_command()
            self.tool_row.set_subtitle(
                _("ZIP and TAR archives still work. Install 7-Zip for 7z, RAR, ISO and more: {command}").format(
                    command=command) if command else
                _("ZIP and TAR archives still work. Install 7-Zip for 7z, RAR, ISO and more."))
            if command:
                self.tool_row.add_suffix(self._copy_button(command))

        icon = Gtk.Image()
        if not has_python:
            icon.set_from_icon_name("dialog-warning-symbolic")
            icon.add_css_class("warning")
            self.files_row.set_title(_("The Files integration needs nautilus-python"))
            family = tool.distro_family()
            command = {"arch": "sudo pacman -S --needed nautilus-python",
                       "debian": "sudo apt install python3-nautilus",
                       "fedora": "sudo dnf install nautilus-python",
                       "suse": "sudo zypper install python3-nautilus"}.get(family)
            self.files_row.set_subtitle(command or _("Install the nautilus-python package"))
            if command:
                self.files_row.add_suffix(self._copy_button(command))
        elif extension is None:
            icon.set_from_icon_name("dialog-warning-symbolic")
            icon.add_css_class("warning")
            self.files_row.set_title(_("The Files extension is not installed"))
            self.files_row.set_subtitle(_("Run the installer again"))
        elif needs_restart:
            icon.set_from_icon_name("view-refresh-symbolic")
            icon.add_css_class("accent")
            self.files_row.set_title(_("Restart Files to load the 7-Zip menu"))
            self.files_row.set_subtitle(_("Open windows of Files will be closed"))
            button = Gtk.Button(label=_("_Restart Files"), use_underline=True, valign=Gtk.Align.CENTER)
            button.add_css_class("pill")
            button.connect("clicked", self._restart_files)
            self.files_row.add_suffix(button)
        else:
            icon.set_from_icon_name("object-select-symbolic")
            icon.add_css_class("success")
            self.files_row.set_title(_("The 7-Zip menu is active in Files"))
            self.files_row.set_subtitle(_("Right-click any file and open the “7-Zip” submenu"))
            self.files_row.set_tooltip_text(extension)
        self.files_row.add_prefix(icon)

    def _copy_button(self, text: str) -> Gtk.Button:
        button = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Copy “{command}”").format(command=text))
        button.add_css_class("flat")

        def copy(b) -> None:
            copy_to_clipboard(b, text)
            self.toasts.add_toast(Adw.Toast(title=_("Command copied")))

        button.connect("clicked", copy)
        return button

    def _restart_files(self, button) -> None:
        button.set_sensitive(False)
        try:
            subprocess.run(["nautilus", "-q"], timeout=10, check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

        def reopen() -> bool:
            try:
                Gio.Subprocess.new(["nautilus", "--new-window"], Gio.SubprocessFlags.NONE)
            except GLib.Error:
                pass
            self.files_row.set_title(_("The 7-Zip menu is active in Files"))
            self.files_row.set_subtitle(_("Files was restarted"))
            button.set_visible(False)
            return False

        GLib.timeout_add(800, reopen)

    def _on_path_apply(self, row) -> None:
        value = row.get_text().strip()
        if value and not tool.candidates(value):
            row.add_css_class("error")
            self.toasts.add_toast(Adw.Toast(title=_("That program could not be found")))
            return
        row.remove_css_class("error")
        self.settings.set("sevenzip_path", value)
        self.toasts.add_toast(Adw.Toast(title=_("Saved")))

    def _open_archive(self) -> None:
        dialog = Gtk.FileDialog(title=_("Open Archive"), modal=True)
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        archives = Gtk.FileFilter(name=_("Archives"))
        from .. import formats

        for fmt in formats.FORMATS:
            for mime in fmt.mime_types:
                archives.add_mime_type(mime)
            for ext in fmt.extensions:
                archives.add_suffix(ext)
        everything = Gtk.FileFilter(name=_("All files"))
        everything.add_pattern("*")
        filters.append(archives)
        filters.append(everything)
        dialog.set_filters(filters)
        dialog.set_default_filter(archives)

        def done(d, result) -> None:
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            if file is not None and file.get_path():
                from .browser import open_archive

                open_archive(self.app, file.get_path())

        dialog.open(self, None, done)

    def _confirm_reset(self) -> None:
        from .prompts import alert, present

        dialog = alert(self, _("Reset All Settings?"), _("Menu items, extraction options and remembered dialog "
                                                         "choices go back to their defaults."))
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("reset", _("_Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_d, response) -> None:
            if response == "reset":
                self.settings.reset()
                self._build()
                self.toasts.add_toast(Adw.Toast(title=_("Settings were reset")))

        dialog.connect("response", on_response)
        present(dialog, self)


def show_about(app, parent: Gtk.Window | None) -> None:
    from .. import ISSUES, WEBSITE, __version__

    kwargs = dict(
        application_name=_("7-Zip for Files"),
        application_icon=APP_ID,
        developer_name="Jire",
        version=__version__,
        website=WEBSITE,
        issue_url=ISSUES,
        license_type=Gtk.License.MIT_X11,
        comments=_("The full 7-Zip right-click menu for GNOME Files: open, extract, test, compress and checksum "
                   "archives."),
        copyright="© 2026 Jire",
    )
    if hasattr(Adw, "AboutDialog"):
        about = Adw.AboutDialog(**kwargs)
        about.add_acknowledgement_section(_("Powered by"), ["7-Zip by Igor Pavlov https://www.7-zip.org"])
        about.add_legal_section("7-Zip", "© Igor Pavlov", Gtk.License.LGPL_2_1, None)
        about.present(parent)
    else:  # pragma: no cover - libadwaita < 1.5
        about = Adw.AboutWindow(transient_for=parent, **kwargs)
        about.present()
