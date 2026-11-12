# SPDX-License-Identifier: MIT
"""Alert dialogs: password, file conflicts, missing 7-Zip, errors."""

from __future__ import annotations

import os

from gi.repository import Adw, Gtk

from .. import tool
from ..engines.base import Answer, ErrorKind, FileDescription
from ..i18n import _
from .common import content_icon, copy_to_clipboard, display_folder, format_size, format_time


def alert(parent: Gtk.Widget, heading: str, body: str = "", *, body_markup: bool = False) -> Adw.AlertDialog:
    """Create an alert dialog (Adw.AlertDialog, or MessageDialog on libadwaita < 1.5)."""
    if hasattr(Adw, "AlertDialog"):
        dialog = Adw.AlertDialog(heading=heading, body=body, body_use_markup=body_markup)
    else:  # pragma: no cover - libadwaita 1.4
        dialog = Adw.MessageDialog(heading=heading, body=body, body_use_markup=body_markup,
                                   transient_for=parent.get_root() if parent else None, modal=True)
    dialog._parent = parent
    return dialog


def present(dialog, parent: Gtk.Widget | None = None) -> None:
    parent = parent or getattr(dialog, "_parent", None)
    if hasattr(Adw, "AlertDialog") and isinstance(dialog, Adw.AlertDialog):
        dialog.present(parent)
    else:  # pragma: no cover
        dialog.present()


def _boxed(*rows: Gtk.Widget) -> Gtk.ListBox:
    box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    box.add_css_class("boxed-list")
    for row in rows:
        box.append(row)
    return box


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


def ask_password(parent: Gtk.Widget, archive_name: str, retry: bool, done) -> None:
    if retry:
        heading = _("Wrong Password")
        body = _("The password for “{name}” is incorrect. Try again.").format(name=archive_name)
    else:
        heading = _("Password Required")
        body = _("“{name}” is encrypted. Enter its password to continue.").format(name=archive_name)
    dialog = alert(parent, heading, body)
    entry = Adw.PasswordEntryRow(title=_("Password"))
    if retry:
        entry.add_css_class("error")
    dialog.set_extra_child(_boxed(entry))
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("unlock", _("_Unlock"))
    dialog.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("unlock")
    dialog.set_close_response("cancel")
    dialog.set_response_enabled("unlock", False)

    def on_changed(_entry) -> None:
        entry.remove_css_class("error")
        dialog.set_response_enabled("unlock", bool(entry.get_text()))

    entry.connect("changed", on_changed)
    entry.set_activates_default(True)  # Enter = "Unlock" (only while it is enabled)
    answered = {"done": False}

    def on_response(_dialog, response: str) -> None:
        if answered["done"]:
            return
        answered["done"] = True
        done(entry.get_text() if response == "unlock" else None)

    dialog.connect("response", on_response)
    present(dialog, parent)
    entry.grab_focus()


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def _describe(info: FileDescription) -> str:
    if info.is_dir:
        return format_time(info.mtime)
    return _("{size} · {date}").format(size=format_size(info.size), date=format_time(info.mtime))


def ask_overwrite(parent: Gtk.Widget, existing: FileDescription, incoming: FileDescription, done) -> None:
    name = os.path.basename(existing.path)
    folder_path = os.path.dirname(existing.path)
    if folder_path == os.path.expanduser("~"):
        folder = display_folder(folder_path)
    else:
        folder = os.path.basename(folder_path) or folder_path
    if existing.is_dir:
        heading = _("Replace Folder “{name}”?").format(name=name)
        body = _("A folder with the same name already exists in “{folder}”.").format(folder=folder)
    else:
        heading = _("Replace “{name}”?").format(name=name)
        body = _("A file with the same name already exists in “{folder}”.").format(folder=folder)
    dialog = alert(parent, heading, body)

    existing_row = Adw.ActionRow(title=_("Existing"), subtitle=_describe(existing))
    incoming_row = Adw.ActionRow(title=_("From the archive"), subtitle=_describe(incoming))
    for row, info in ((existing_row, existing), (incoming_row, incoming)):
        icon = Gtk.Image.new_from_gicon(content_icon(info.path, is_dir=info.is_dir))
        icon.set_pixel_size(32)
        row.add_prefix(icon)
        row.add_css_class("property")
    if existing.mtime and incoming.mtime and not existing.is_dir:
        newer = incoming_row if incoming.mtime > existing.mtime else existing_row
        if abs(incoming.mtime - existing.mtime) >= 2:
            tag = Gtk.Label(label=_("Newer"), valign=Gtk.Align.CENTER)
            tag.add_css_class("tag")
            tag.add_css_class("accent")
            newer.add_suffix(tag)
    apply_all = Gtk.CheckButton(label=_("_Apply this action to all files and folders"), use_underline=True)
    apply_all.set_margin_top(6)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(_boxed(existing_row, incoming_row))
    box.append(apply_all)
    dialog.set_extra_child(box)

    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("skip", _("_Skip"))
    dialog.add_response("keep", _("_Keep Both"))
    dialog.add_response("replace", _("_Replace"))
    dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("replace")
    dialog.set_close_response("cancel")
    answers = {"cancel": Answer.CANCEL, "skip": Answer.SKIP, "keep": Answer.KEEP_BOTH, "replace": Answer.REPLACE}
    dialog.connect("response", lambda _d, r: done((answers.get(r, Answer.CANCEL), apply_all.get_active())))
    present(dialog, parent)


# ---------------------------------------------------------------------------
# Missing 7-Zip / failures
# ---------------------------------------------------------------------------


def install_hint(parent: Gtk.Widget, message: str, *, rar: bool = False, done=None) -> None:
    command = tool.install_command(rar=rar)
    if rar:
        heading = _("RAR Support Is Missing")
        body = message + "\n\n" + _("Install the RAR plugin for 7-Zip, then try again.")
    else:
        heading = _("7-Zip Is Not Installed")
        body = message + "\n\n" + _(
            "ZIP and TAR archives work without it, but 7z, RAR, ISO and dozens of other formats need 7-Zip.")
    dialog = alert(parent, heading, body)
    if command:
        row = Adw.ActionRow(title=_("Install command"), subtitle=command, subtitle_selectable=True)
        row.add_css_class("property")
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Copy"))
        copy.add_css_class("flat")
        copy.connect("clicked", lambda b: (copy_to_clipboard(b, command), b.set_icon_name("object-select-symbolic")))
        row.add_suffix(copy)
        dialog.set_extra_child(_boxed(row))
    dialog.add_response("close", _("_Close"))
    if command:
        dialog.add_response("copy", _("Copy _Command"))
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("copy")

    def on_response(_d, response):
        if response == "copy" and command:
            copy_to_clipboard(parent, command)
        if done:
            done(response)

    dialog.set_close_response("close")
    dialog.connect("response", on_response)
    present(dialog, parent)


def error_title(kind: ErrorKind | None) -> str:
    return {
        ErrorKind.WRONG_PASSWORD: _("Wrong Password"),
        ErrorKind.NOT_ARCHIVE: _("Cannot Open Archive"),
        ErrorKind.UNSUPPORTED: _("Unsupported Archive"),
        ErrorKind.DAMAGED: _("Archive Is Damaged"),
        ErrorKind.TRUNCATED: _("Archive Is Incomplete"),
        ErrorKind.MISSING_VOLUME: _("Missing Archive Part"),
        ErrorKind.DISK_FULL: _("Not Enough Disk Space"),
        ErrorKind.PERMISSION: _("Permission Denied"),
        ErrorKind.NEEDS_7ZIP: _("7-Zip Is Not Installed"),
        ErrorKind.NEEDS_RAR: _("RAR Support Is Missing"),
        ErrorKind.NOT_FOUND: _("File Not Found"),
        ErrorKind.MEMORY: _("Not Enough Memory"),
    }.get(kind, _("Something Went Wrong"))
