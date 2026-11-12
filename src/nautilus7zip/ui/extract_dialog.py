# SPDX-License-Identifier: MIT
"""The "Extract Files…" dialog (7-Zip's Extract dialog, the GNOME way)."""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, GLib, Gtk

from .. import formats, jobs
from ..engines.base import OverwriteMode
from ..i18n import _, ngettext
from .common import close_on_escape, display_folder

PATH_MODES = (
    ("full", _("Full pathnames"), _("Recreate the folders stored in the archive")),
    ("none", _("No pathnames"), _("Put every file directly into the destination")),
)
OVERWRITE_MODES = (
    (OverwriteMode.ASK, _("Ask before overwrite")),
    (OverwriteMode.OVERWRITE, _("Overwrite without prompt")),
    (OverwriteMode.SKIP, _("Skip existing files")),
    (OverwriteMode.RENAME_NEW, _("Auto rename")),
    (OverwriteMode.RENAME_EXISTING, _("Auto rename existing files")),
)


class ExtractDialog(Adw.ApplicationWindow):
    def __init__(self, app, archives: list[str], *, members: list[str] | None = None, strip_prefix: str = "",
                 subfolder: bool | None = None) -> None:
        super().__init__(application=app, title=_("Extract"))
        self.app = app
        self.archives = archives
        self.members = members
        self.strip_prefix = strip_prefix
        self.settings = app.settings
        self.set_default_size(540, 700)
        self.set_size_request(360, 400)

        first = archives[0]
        self.destination = os.path.dirname(os.path.abspath(first))
        single = len(archives) == 1
        if members:
            subtitle = ngettext("{n} item from “{name}”", "{n} items from “{name}”", len(members)).format(
                n=len(members), name=os.path.basename(first))
        elif single:
            subtitle = os.path.basename(first)
        else:
            subtitle = ngettext("{n} archive", "{n} archives", len(archives)).format(n=len(archives))

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title=_("Extract"), subtitle=subtitle))
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        self.extract_button = Gtk.Button(label=_("_Extract"), use_underline=True)
        self.extract_button.add_css_class("suggested-action")
        self.extract_button.connect("clicked", self._on_extract)
        header.pack_end(self.extract_button)
        self.set_default_widget(self.extract_button)

        page = Adw.PreferencesPage()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(page)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        # Destination ---------------------------------------------------------
        dest_group = Adw.PreferencesGroup(title=_("Destination"))
        self.dest_row = Adw.ActionRow(title=_("Extract to"), activatable=True)
        self.dest_row.add_css_class("property")
        self.dest_row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
        choose = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Choose a folder"))
        choose.add_css_class("flat")
        choose.connect("clicked", lambda _b: self._choose_folder())
        self.dest_row.add_suffix(choose)
        self.dest_row.connect("activated", lambda _r: self._choose_folder())
        dest_group.add(self.dest_row)

        recent = [d for d in self.settings.get("extract.recent_destinations", []) if os.path.isdir(d)]
        recent = [d for d in recent if d != self.destination][:5]
        if recent:
            expander = Adw.ExpanderRow(title=_("Recent folders"))
            expander.add_prefix(Gtk.Image.new_from_icon_name("document-open-recent-symbolic"))
            for path in recent:
                row = Adw.ActionRow(title=os.path.basename(path) or path, subtitle=display_folder(path),
                                    activatable=True)
                row.connect("activated", lambda _r, p=path: self._set_destination(p))
                expander.add_row(row)
            dest_group.add(expander)

        if subfolder is None:
            subfolder = not members
        self.subfolder_row = Adw.SwitchRow(active=subfolder)
        if single:
            stem = formats.archive_stem(os.path.basename(first))
            self.subfolder_row.set_title(_("Create a folder"))
            self.name_row = Adw.EntryRow(title=_("Folder name"), text=stem)
            self.name_row.connect("changed", lambda _r: self._update())
        else:
            self.subfolder_row.set_title(_("Create a folder for each archive"))
            self.subfolder_row.set_subtitle(_("Named after the archive"))
            self.name_row = None
        self.subfolder_row.connect("notify::active", lambda *_a: self._update())
        dest_group.add(self.subfolder_row)
        if self.name_row is not None:
            dest_group.add(self.name_row)
        page.add(dest_group)

        # Options ---------------------------------------------------------------
        options = Adw.PreferencesGroup(title=_("Options"))
        self.path_row = Adw.ComboRow(title=_("Path mode"), model=Gtk.StringList.new([m[1] for m in PATH_MODES]))
        remembered_path = self.settings.get("extract.path_mode", "full")
        self.path_row.set_selected(next((i for i, m in enumerate(PATH_MODES) if m[0] == remembered_path), 0))
        self.path_row.connect("notify::selected", lambda *_a: self._update())
        options.add(self.path_row)

        self.root_row = Adw.SwitchRow(title=_("Eliminate duplication of root folder"),
                                      active=bool(self.settings.get("eliminate_root_duplication", True)))
        self.root_row.set_subtitle(_("Avoids “name/name/…” when the archive already has a “name” folder"))
        options.add(self.root_row)

        self.overwrite_row = Adw.ComboRow(title=_("Overwrite mode"),
                                          model=Gtk.StringList.new([m[1] for m in OVERWRITE_MODES]))
        remembered = self.settings.get("extract.overwrite", "ask")
        self.overwrite_row.set_selected(next((i for i, m in enumerate(OVERWRITE_MODES) if m[0].value == remembered),
                                             0))
        options.add(self.overwrite_row)
        page.add(options)

        # Password --------------------------------------------------------------
        pw_group = Adw.PreferencesGroup(title=_("Password"),
                                        description=_("Only needed for encrypted archives. You will be asked if "
                                                      "one is required."))
        self.password_row = Adw.PasswordEntryRow(title=_("Password"))
        self.password_row.connect("entry-activated", self._on_extract)
        pw_group.add(self.password_row)
        page.add(pw_group)

        self._set_destination(self.destination)
        self._update()
        close_on_escape(self)

    # -----------------------------------------------------------------------
    def _set_destination(self, path: str) -> None:
        self.destination = path
        self.dest_row.set_subtitle(display_folder(path))
        self.dest_row.set_tooltip_text(path)
        self._update()

    def _choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title=_("Extract To"), modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(self.destination))

        def on_done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is not None and folder.get_path():
                self._set_destination(folder.get_path())

        dialog.select_folder(self, None, on_done)

    def _target_preview(self) -> str:
        base = self.destination
        if self.subfolder_row.get_active():
            if self.name_row is not None:
                name = self.name_row.get_text().strip() or formats.archive_stem(os.path.basename(self.archives[0]))
                return os.path.join(base, name)
            return os.path.join(base, "…")
        return base

    def _update(self) -> None:
        sub = self.subfolder_row.get_active()
        if self.name_row is not None:
            self.name_row.set_visible(sub)
            name = self.name_row.get_text().strip()
            bad = sub and (not name or "/" in name or name in (".", ".."))
            if bad:
                self.name_row.add_css_class("error")
            else:
                self.name_row.remove_css_class("error")
            self.extract_button.set_sensitive(not bad)
        self.root_row.set_sensitive(sub)
        self.path_row.set_subtitle(PATH_MODES[self.path_row.get_selected()][2])
        self.dest_row.set_subtitle(display_folder(self._target_preview()))

    def _on_extract(self, *_args) -> None:
        if not self.extract_button.get_sensitive():
            return
        path_mode = PATH_MODES[self.path_row.get_selected()][0]
        overwrite = OVERWRITE_MODES[self.overwrite_row.get_selected()][0]
        sub = self.subfolder_row.get_active()
        request = jobs.ExtractRequest(
            archives=self.archives,
            mode="custom",
            destination=self.destination,
            subfolder=sub,
            folder_name=self.name_row.get_text().strip() if (self.name_row is not None and sub) else None,
            eliminate_root=self.root_row.get_active(),
            overwrite=overwrite,
            flatten=path_mode == "none",
            password=self.password_row.get_text() or None,
            members=self.members,
            strip_prefix=self.strip_prefix,
        )
        self.settings.set("extract.path_mode", path_mode)
        self.settings.set("extract.overwrite", overwrite.value)
        recent = [self.destination] + [d for d in self.settings.get("extract.recent_destinations", [])
                                       if d != self.destination]
        self.settings.set("extract.recent_destinations", recent[:8])
        from .actions import start_extract

        start_extract(self.app, request)
        self.destroy()
