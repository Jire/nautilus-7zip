# SPDX-License-Identifier: MIT
"""Checksum results (CRC SHA) and checksum-file verification windows."""

from __future__ import annotations

import os
import re

from gi.repository import Adw, Gio, GLib, Gtk

from .. import formats, hashalgos, jobs
from ..i18n import _, ngettext
from .common import close_on_escape, content_icon, copy_to_clipboard, format_size

MAX_ROWS = 250


def _copy_button(text_func, tooltip: str) -> Gtk.Button:
    button = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    button.add_css_class("flat")

    def on_click(b) -> None:
        copy_to_clipboard(b, text_func())
        b.set_icon_name("object-select-symbolic")
        GLib.timeout_add(1200, lambda: (b.set_icon_name("edit-copy-symbolic"), False)[1])

    button.connect("clicked", on_click)
    return button


class ChecksumWindow(Adw.ApplicationWindow):
    def __init__(self, app, result: jobs.ChecksumResult, paths: list[str]) -> None:
        super().__init__(application=app, title=_("Checksums"))
        self.app = app
        self.result = result
        self.paths = paths
        self.set_default_size(760, 600)
        self.set_size_request(360, 360)
        self.files = [r for r in result.rows if not r.is_dir]
        algorithms = [hashalgos.BY_ID[a] for a in result.algorithms]
        label = algorithms[0].label if len(algorithms) == 1 else ngettext(
            "{n} algorithm", "{n} algorithms", len(algorithms)).format(n=len(algorithms))
        subtitle = _("{algorithms} · {files}").format(
            algorithms=label, files=ngettext("{n} file", "{n} files", len(self.files)).format(n=len(self.files)))

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Checksums"), subtitle=subtitle))
        copy_all = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text=_("Copy all checksums"))
        copy_all.connect("clicked", self._on_copy_all)
        header.pack_end(copy_all)
        save = Gtk.MenuButton(icon_name="document-save-symbolic", tooltip_text=_("Save as a checksum file"))
        menu = Gio.Menu()
        for algo in algorithms:
            menu.append(_("Save {algorithm} File…").format(algorithm=algo.label), f"win.save::{algo.id}")
        save.set_menu_model(menu)
        header.pack_end(save)
        action = Gio.SimpleAction.new("save", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, v: self._save(v.get_string()))
        self.add_action(action)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        compare = Adw.PreferencesGroup(title=_("Compare"),
                                       description=_("Paste a checksum from a website to check it against "
                                                     "these files."))
        self.compare_row = Adw.EntryRow(title=_("Expected checksum"))
        self.compare_row.add_prefix(Gtk.Image.new_from_icon_name("edit-find-symbolic"))
        paste = Gtk.Button(icon_name="edit-paste-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Paste"))
        paste.add_css_class("flat")
        paste.connect("clicked", self._on_paste)
        self.compare_row.add_suffix(paste)
        self.compare_row.connect("changed", lambda _r: self._compare())
        compare.add(self.compare_row)
        self.compare_status = Adw.ActionRow(visible=False)
        self.compare_icon = Gtk.Image()
        self.compare_status.add_prefix(self.compare_icon)
        compare.add(self.compare_status)
        page.add(compare)

        self.digest_rows: list[tuple[Adw.ActionRow, str, str, str]] = []  # row, digest, file, algorithm
        if len(self.files) <= MAX_ROWS:
            for row in self.files:
                group = Adw.PreferencesGroup(title=GLib.markup_escape_text(row.name))
                if row.error:
                    group.set_description(row.error)
                else:
                    group.set_description(format_size(row.size) if row.size is not None else None)
                icon = Gtk.Image.new_from_gicon(content_icon(row.path))
                icon.set_pixel_size(24)
                group.set_header_suffix(icon)
                for algo in algorithms:
                    digest = row.digests.get(algo.id)
                    if not digest:
                        continue
                    action_row = Adw.ActionRow(title=algo.label, subtitle=digest, subtitle_selectable=True)
                    action_row.add_css_class("property")
                    action_row.add_css_class("hash-row")
                    action_row.add_suffix(_copy_button(lambda d=digest: d, _("Copy")))
                    group.add(action_row)
                    self.digest_rows.append((action_row, digest, row.name, algo.label))
                page.add(group)
        else:
            group = Adw.PreferencesGroup(title=ngettext("{n} file", "{n} files", len(self.files)).format(
                n=len(self.files)))
            view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE, top_margin=10,
                                bottom_margin=10, left_margin=12, right_margin=12)
            view.get_buffer().set_text(self._listing())
            scroller = Gtk.ScrolledWindow(min_content_height=360)
            scroller.set_child(view)
            scroller.add_css_class("card")
            group.add(scroller)
            page.add(group)

        if result.data_sums:
            group = Adw.PreferencesGroup(title=_("Combined"),
                                         description=_("7-Zip’s checksums of all files together"))
            for algo in algorithms:
                for title, sums in ((_("{algorithm} for data"), result.data_sums),
                                    (_("{algorithm} for data and names"), result.names_sums)):
                    value = sums.get(algo.id)
                    if value:
                        r = Adw.ActionRow(title=title.format(algorithm=algo.label), subtitle=value,
                                          subtitle_selectable=True)
                        r.add_css_class("property")
                        r.add_css_class("hash-row")
                        r.add_suffix(_copy_button(lambda v=value: v, _("Copy")))
                        group.add(r)
            page.add(group)

        close_on_escape(self)
        footer = Adw.PreferencesGroup()
        engine = Gtk.Label(label=_("Calculated with {engine}").format(engine=result.engine), xalign=0.5)
        engine.add_css_class("dim-label")
        engine.add_css_class("caption")
        footer.add(engine)
        page.add(footer)

    def _listing(self, algorithm: str | None = None) -> str:
        lines = []
        algos = [algorithm] if algorithm else self.result.algorithms
        for row in self.files:
            for aid in algos:
                digest = row.digests.get(aid)
                if digest:
                    prefix = f"{hashalgos.BY_ID[aid].label}  " if len(algos) > 1 else ""
                    lines.append(f"{prefix}{digest}  {row.name}")
        return "\n".join(lines)

    def _on_copy_all(self, button) -> None:
        copy_to_clipboard(button, self._listing())
        self.toasts.add_toast(Adw.Toast(title=_("Checksums copied")))

    def _on_paste(self, _button) -> None:
        clipboard = self.get_clipboard()

        def done(cb, result):
            try:
                text = cb.read_text_finish(result)
            except GLib.Error:
                return
            if text:
                self.compare_row.set_text(text.strip())

        clipboard.read_text_async(None, done)

    def _compare(self) -> None:
        text = re.sub(r"[\s:-]", "", self.compare_row.get_text()).lower()
        for row, _digest, _name, _algo in self.digest_rows:
            row.remove_css_class("success")
        if not text:
            self.compare_status.set_visible(False)
            return
        matches = [(row, name, algo) for row, digest, name, algo in self.digest_rows if digest.lower() == text]
        self.compare_status.set_visible(True)
        self.compare_status.remove_css_class("success")
        self.compare_status.remove_css_class("error")
        if not matches and len(self.digest_rows) == 0:
            # Large lists: search the raw results.
            for row in self.files:
                for aid, digest in row.digests.items():
                    if digest.lower() == text:
                        matches.append((None, row.name, hashalgos.BY_ID[aid].label))
        if matches:
            row, name, algo = matches[0]
            self.compare_icon.set_from_icon_name("object-select-symbolic")
            self.compare_status.set_title(_("Match"))
            self.compare_status.set_subtitle(_("Same as the {algorithm} of “{name}”").format(algorithm=algo,
                                                                                             name=name))
            self.compare_status.add_css_class("success")
            if row is not None:
                row.add_css_class("success")
        else:
            self.compare_icon.set_from_icon_name("dialog-error-symbolic")
            self.compare_status.set_title(_("No match"))
            self.compare_status.set_subtitle(_("None of the checksums above is equal to this value"))
            self.compare_status.add_css_class("error")

    def _save(self, algorithm: str) -> None:
        algo = hashalgos.BY_ID[algorithm]
        if len(self.paths) == 1 and not os.path.isdir(self.paths[0]):
            name = os.path.basename(self.paths[0]) + "." + algo.file_extension
        else:
            name = formats.archive_name_for(self.paths) + "." + algo.file_extension
        dialog = Gtk.FileDialog(title=_("Save Checksums"), modal=True, initial_name=name)
        dialog.set_initial_folder(Gio.File.new_for_path(formats.common_parent(self.paths)))

        def done(d, result):
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return
            if file is None or not file.get_path():
                return
            try:
                jobs.write_checksum_file(self.result, algorithm, file.get_path())
            except OSError as exc:
                self.toasts.add_toast(Adw.Toast(title=exc.strerror or str(exc)))
                return
            self.toasts.add_toast(Adw.Toast(title=_("Saved “{name}”").format(name=os.path.basename(file.get_path()))))

        dialog.save(self, None, done)


class VerifyWindow(Adw.ApplicationWindow):
    def __init__(self, app, checksum_file: str, rows: list[jobs.VerifyRow]) -> None:
        super().__init__(application=app, title=_("Verify Checksums"))
        self.set_default_size(640, 560)
        self.set_size_request(360, 360)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Verify Checksums"), subtitle=os.path.basename(checksum_file)))
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        self.set_content(toolbar)

        ok = [r for r in rows if r.status == "ok"]
        failed = [r for r in rows if r.status == "failed"]
        missing = [r for r in rows if r.status == "missing"]
        errors = [r for r in rows if r.status == "error"]
        if not failed and not missing and not errors:
            icon, style = "object-select-symbolic", "success"
            title = ngettext("The File Is Intact", "All {n} Files Are Intact", len(ok)).format(n=len(ok))
            description = _("Every checksum matches.")
        else:
            icon, style = "dialog-error-symbolic", "error"
            problems = len(failed) + len(missing) + len(errors)
            title = ngettext("{n} Problem Found", "{n} Problems Found", problems).format(n=problems)
            parts = []
            if failed:
                parts.append(ngettext("{n} file is corrupted or changed", "{n} files are corrupted or changed",
                                      len(failed)).format(n=len(failed)))
            if missing:
                parts.append(ngettext("{n} file is missing", "{n} files are missing", len(missing)).format(
                    n=len(missing)))
            if errors:
                parts.append(ngettext("{n} file could not be checked", "{n} files could not be checked",
                                      len(errors)).format(n=len(errors)))
            description = ", ".join(parts) + "."
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=6)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(64)
        image.add_css_class(style)
        heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER)
        heading.add_css_class("title-1")
        body = Gtk.Label(label=description, wrap=True, justify=Gtk.Justification.CENTER)
        body.add_css_class("dim-label")
        for widget in (image, heading, body):
            header_box.append(widget)
        group = Adw.PreferencesGroup()
        group.add(header_box)
        page.add(group)

        listing = Adw.PreferencesGroup(title=_("Files"))
        order = {"failed": 0, "missing": 1, "error": 2, "ok": 3}
        for row in sorted(rows, key=lambda r: (order.get(r.status, 9), r.name))[:2000]:
            algo = row.algorithm.upper().replace("SHA", "SHA-").replace("SHA-3-", "SHA3-") if row.algorithm else "?"
            if row.status == "ok":
                subtitle, icon_name, css = _("{algorithm} matches").format(algorithm=algo), "object-select-symbolic", \
                    "success"
            elif row.status == "failed":
                subtitle = _("Expected {expected}, got {actual}").format(expected=row.expected, actual=row.actual)
                icon_name, css = "dialog-error-symbolic", "error"
            elif row.status == "missing":
                subtitle, icon_name, css = _("File not found"), "dialog-question-symbolic", "warning"
            else:
                subtitle, icon_name, css = row.error or _("Could not be read"), "dialog-warning-symbolic", "warning"
            action_row = Adw.ActionRow(title=GLib.markup_escape_text(row.name),
                                       subtitle=GLib.markup_escape_text(subtitle), subtitle_selectable=True)
            image = Gtk.Image.new_from_icon_name(icon_name)
            image.add_css_class(css)
            action_row.add_prefix(image)
            listing.add(action_row)
        page.add(listing)
        close_on_escape(self)
