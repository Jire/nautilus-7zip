# SPDX-License-Identifier: MIT
"""The "Add to Archive…" dialog: every 7-Zip option, grouped so the common
ones are up front and the expert ones are one click away."""

from __future__ import annotations

import os
import re
import shlex
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from .. import formats, jobs, tool
from ..i18n import _, ngettext
from .common import close_on_escape, display_folder, format_size
from .prompts import alert, present

# id, label, description
FORMAT_INFO = {
    "7z": ("7z", _("Best compression, strong AES-256 encryption")),
    "zip": ("ZIP", _("Opens on every computer and phone")),
    "tar": ("TAR", _("No compression, keeps Unix permissions")),
    "tar.gz": ("TAR.GZ", _("The classic Linux tarball")),
    "tar.xz": ("TAR.XZ", _("Smaller than .tar.gz, slower to create")),
    "tar.zst": ("TAR.ZST", _("Very fast, compresses well (Zstandard)")),
    "tar.bz2": ("TAR.BZ2", _("Older tarball format")),
    "wim": ("WIM", _("Windows Imaging Format")),
    "gz": ("GZIP", _("Compresses a single file")),
    "xz": ("XZ", _("Compresses a single file")),
    "bz2": ("BZIP2", _("Compresses a single file")),
    "zst": ("ZSTD", _("Compresses a single file")),
}
FORMAT_ORDER = ("7z", "zip", "tar.gz", "tar.xz", "tar.zst", "tar.bz2", "tar", "wim", "gz", "xz", "bz2", "zst")
NEEDS_7ZIP = ("7z", "wim")

LEVELS = ((0, _("Store")), (1, _("Fastest")), (3, _("Fast")), (5, _("Normal")), (7, _("Maximum")), (9, _("Ultra")))

METHODS = {
    "7z": ("LZMA2", "LZMA", "PPMd", "BZip2"),
    "zip": ("Deflate", "Deflate64", "BZip2", "LZMA", "PPMd"),
}
BUILTIN_ZIP_METHODS = ("Deflate", "BZip2", "LZMA")

KB, MB, GB = 1 << 10, 1 << 20, 1 << 30
DICTIONARIES = (64 * KB, 256 * KB, 1 * MB, 2 * MB, 4 * MB, 8 * MB, 16 * MB, 32 * MB, 64 * MB, 128 * MB, 256 * MB,
                512 * MB, 1 * GB, 1536 * MB)
DEFAULT_DICT = {1: 256 * KB, 3: 4 * MB, 5: 32 * MB, 7: 128 * MB, 9: 256 * MB}
WORD_SIZES = (8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 273)
SOLID_SIZES = ("1m", "2m", "4m", "8m", "16m", "32m", "64m", "128m", "256m", "512m", "1g", "2g", "4g", "8g", "16g",
               "32g", "64g")
VOLUMES = (
    (None, _("Don’t split")),
    ("10m", _("10 MB")),
    ("100m", _("100 MB")),
    ("650m", _("650 MB — CD")),
    ("700m", _("700 MB — CD")),
    ("1g", _("1 GB")),
    ("4092m", _("4 GB — FAT32")),
    ("4480m", _("4.7 GB — DVD")),
    ("8128m", _("8.5 GB — DVD DL")),
    ("23040m", _("25 GB — Blu-ray")),
    ("custom", _("Custom…")),
)
UPDATE_MODES = (
    ("add", _("Add and replace files")),
    ("update", _("Update and add files")),
    ("freshen", _("Freshen existing files")),
    ("sync", _("Synchronize files")),
)
PATH_MODES = (
    ("relative", _("Relative pathnames")),
    ("full", _("Full pathnames")),
    ("absolute", _("Absolute pathnames")),
)


def _size_label(value: int) -> str:
    if value >= GB and value % GB == 0:
        return f"{value // GB} GB"
    if value >= MB:
        return f"{value // MB} MB"
    return f"{value // KB} KB"


def _sevenzip_size(value: int) -> str:
    if value % GB == 0:
        return f"{value // GB}g"
    if value % MB == 0:
        return f"{value // MB}m"
    return f"{value // KB}k"


def lzma_memory(dictionary: int, threads: int, level: int, lzma2: bool = True) -> tuple[int, int]:
    """Estimate (compression, decompression) memory like 7-Zip's dialog does."""
    hs = dictionary - 1
    for shift in (1, 2, 4, 8):
        hs |= hs >> shift
    hs >>= 1
    if hs >= 1 << 24:
        hs >>= 1
    hs |= (1 << 16) - 1
    if level < 5:
        hs |= (256 << 10) - 1
    hs += 1
    size1 = hs * 4 + dictionary * 4 + (dictionary * 4 if level >= 5 else 0) + (2 << 20)
    threads_per_block = 1
    if threads > 1 and level >= 5:
        size1 += (2 << 20) + (4 << 20)
        threads_per_block = 2
    blocks = max(threads // threads_per_block, 1)
    if lzma2 and blocks > 1:
        chunk = min(max(dictionary << 2, 1 << 20), 1 << 28)
        chunk = max(chunk, dictionary)
        chunk = (chunk + (1 << 20) - 1) & ~((1 << 20) - 1)
        packs = min(blocks + blocks // 4 + 2, blocks * 2)
        total = blocks * (size1 + chunk) + packs * chunk
    else:
        total = size1
    return total, dictionary + (2 << 20)


def _available_memory() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def parse_volume(text: str) -> str | None:
    """"250M" / "1.5 GB" / "700" (MB) → 7-Zip's "-v" syntax, or None if invalid."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([kKmMgG]?)[bB]?\s*", text)
    if not m:
        return None
    number = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "m").lower()
    factor = {"k": 1, "m": 1024, "g": 1024 * 1024}[unit]
    kib = int(number * factor)
    if kib <= 0:
        return None
    return f"{kib // 1024}m" if kib % 1024 == 0 else f"{kib}k"


class AddDialog(Adw.ApplicationWindow):
    def __init__(self, app, paths: list[str], *, email: bool = False, format_hint: str | None = None) -> None:
        super().__init__(application=app, title=_("Add to Archive"))
        self.app = app
        self.settings = app.settings
        self.paths = [os.path.abspath(p) for p in paths]
        self.email = email
        self.single_file = len(self.paths) == 1 and os.path.isfile(self.paths[0])
        self.location = formats.common_parent(self.paths)
        self.total_size: int | None = None
        self.set_default_size(580, 760)
        self.set_size_request(360, 420)

        override = self.settings.get("sevenzip_path") or None
        self.tool_7z = tool.best_tool(write="7z", override=override)
        self.tool_zip = tool.best_tool(write="zip", override=override)
        self.tool_wim = tool.best_tool(write="wim", override=override)
        self.has_7zip = tool.default_tool(override) is not None
        self.cpu_count = os.cpu_count() or 1

        remembered = self.settings.get("add", {})
        self.formats = [f for f in FORMAT_ORDER if f not in jobs.SINGLE_FILE_FORMATS or self.single_file]
        if not formats.has_python_zstd():
            self.formats = [f for f in self.formats if f not in ("tar.zst", "zst")]
        wanted = format_hint or remembered.get("format", "7z")
        if wanted not in self.formats or not self._format_available(wanted):
            wanted = "7z" if self._format_available("7z") else "zip"

        # Header -------------------------------------------------------------------
        count = len(self.paths)
        subtitle = os.path.basename(self.paths[0]) if count == 1 else \
            ngettext("{n} item", "{n} items", count).format(n=count)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        self.window_title = Adw.WindowTitle(title=_("Compress and Email") if email else _("Add to Archive"),
                                            subtitle=subtitle)
        header.set_title_widget(self.window_title)
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        self.create_button = Gtk.Button(label=_("Create and _Email") if email else _("C_reate"), use_underline=True)
        self.create_button.add_css_class("suggested-action")
        self.create_button.connect("clicked", self._on_create)
        header.pack_end(self.create_button)
        self.set_default_widget(self.create_button)

        page = Adw.PreferencesPage()
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(page)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self._on_banner)
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        # Archive -------------------------------------------------------------------
        self.archive_group = Adw.PreferencesGroup(title=_("Archive"))
        base = formats.archive_name_for(self.paths)
        self.name_row = Adw.EntryRow(title=_("Name"), text=base + jobs.CREATE_FORMATS[wanted][1])
        self.name_row.connect("changed", lambda _r: self._validate())
        self.archive_group.add(self.name_row)
        self.location_row = Adw.ActionRow(title=_("Location"), activatable=True, visible=not email)
        self.location_row.add_css_class("property")
        self.location_row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
        browse = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Choose a folder"))
        browse.add_css_class("flat")
        browse.connect("clicked", lambda _b: self._choose_location())
        self.location_row.add_suffix(browse)
        self.location_row.connect("activated", lambda _r: self._choose_location())
        self.archive_group.add(self.location_row)
        labels = []
        for f in self.formats:
            label = FORMAT_INFO[f][0]
            if not self._format_available(f):
                label = _("{format} (requires 7-Zip)").format(format=label)
            labels.append(label)
        self.format_row = Adw.ComboRow(title=_("Format"), model=Gtk.StringList.new(labels))
        self.format_row.set_selected(self.formats.index(wanted))
        self.format_row.connect("notify::selected", lambda *_a: self._on_format_changed())
        self.archive_group.add(self.format_row)
        page.add(self.archive_group)

        # Compression ----------------------------------------------------------------
        self.compression_group = Adw.PreferencesGroup(title=_("Compression"))
        self.level_row = Adw.ComboRow(title=_("Compression level"),
                                      model=Gtk.StringList.new([label for _v, label in LEVELS]))
        level = int(remembered.get("level", 5))
        self.level_row.set_selected(next((i for i, (v, _l) in enumerate(LEVELS) if v == level), 3))
        self.level_row.connect("notify::selected", lambda *_a: self._refresh())
        self.compression_group.add(self.level_row)

        self.method_row = Adw.ComboRow(title=_("Compression method"))
        self.method_row.connect("notify::selected", lambda *_a: self._refresh(keep_method=True))
        self.compression_group.add(self.method_row)

        self.advanced = Adw.ExpanderRow(title=_("Advanced"), subtitle=_("Dictionary, word size, solid blocks, threads"))
        dictionaries = [_("Automatic")] + [_size_label(d) for d in DICTIONARIES]
        self.dict_row = Adw.ComboRow(title=_("Dictionary size"), model=Gtk.StringList.new(dictionaries))
        self.dict_row.connect("notify::selected", lambda *_a: self._refresh(keep_method=True))
        self.word_row = Adw.ComboRow(title=_("Word size"),
                                     model=Gtk.StringList.new([_("Automatic")] + [str(w) for w in WORD_SIZES]))
        solid_labels = [_("Automatic"), _("Non-solid"),
                        *(s[:-1] + " " + s[-1].upper() + "B" for s in SOLID_SIZES), _("Solid")]
        self.solid_row = Adw.ComboRow(title=_("Solid block size"), model=Gtk.StringList.new(solid_labels))
        thread_labels = [_("Automatic ({n})").format(n=self.cpu_count)] + [str(i) for i in
                                                                           range(1, self.cpu_count * 2 + 1)]
        self.threads_row = Adw.ComboRow(title=_("CPU threads"), model=Gtk.StringList.new(thread_labels))
        threads = int(remembered.get("threads", 0) or 0)
        self.threads_row.set_selected(threads if 0 < threads <= self.cpu_count * 2 else 0)
        self.threads_row.connect("notify::selected", lambda *_a: self._refresh(keep_method=True))
        self.memory_row = Adw.ActionRow(title=_("Memory usage"))
        self.memory_row.add_css_class("property")
        for row in (self.dict_row, self.word_row, self.solid_row, self.threads_row, self.memory_row):
            self.advanced.add_row(row)
        self.compression_group.add(self.advanced)
        page.add(self.compression_group)

        # Encryption --------------------------------------------------------------------
        self.encryption_group = Adw.PreferencesGroup(title=_("Encryption"))
        self.password_row = Adw.PasswordEntryRow(title=_("Password"))
        self.password_row.connect("changed", lambda _r: self._validate())
        self.confirm_row = Adw.PasswordEntryRow(title=_("Confirm password"))
        self.confirm_row.connect("changed", lambda _r: self._validate())
        self.enc_method_row = Adw.ComboRow(title=_("Encryption method"),
                                           model=Gtk.StringList.new(["AES-256", _("ZipCrypto (legacy, weak)")]))
        if remembered.get("zip_encryption") == "ZipCrypto":
            self.enc_method_row.set_selected(1)
        self.encrypt_names_row = Adw.SwitchRow(title=_("Encrypt file names"),
                                               subtitle=_("Hide the list of files until the password is entered"),
                                               active=bool(remembered.get("encrypt_names", False)))
        for row in (self.password_row, self.confirm_row, self.enc_method_row, self.encrypt_names_row):
            self.encryption_group.add(row)
        page.add(self.encryption_group)

        # Options ----------------------------------------------------------------------
        self.options_group = Adw.PreferencesGroup(title=_("Options"))
        self.volume_row = Adw.ComboRow(title=_("Split into volumes"),
                                       model=Gtk.StringList.new([label for _v, label in VOLUMES]))
        self.volume_row.connect("notify::selected", lambda *_a: self._validate())
        self.volume_custom_row = Adw.EntryRow(title=_("Volume size (e.g. 250M, 2G)"), visible=False)
        self.volume_custom_row.connect("changed", lambda _r: self._validate())
        self.update_row = Adw.ComboRow(title=_("Update mode"),
                                       model=Gtk.StringList.new([label for _v, label in UPDATE_MODES]))
        self.update_row.set_selected(next((i for i, (v, _l) in enumerate(UPDATE_MODES)
                                           if v == remembered.get("update_mode")), 0))
        self.path_row = Adw.ComboRow(title=_("Path mode"),
                                     model=Gtk.StringList.new([label for _v, label in PATH_MODES]))
        self.path_row.set_selected(next((i for i, (v, _l) in enumerate(PATH_MODES)
                                         if v == remembered.get("path_mode")), 0))
        self.symlinks_row = Adw.SwitchRow(title=_("Store symbolic links as links"),
                                          subtitle=_("Otherwise the files they point to are stored"),
                                          active=bool(remembered.get("store_symlinks", True)))
        self.hardlinks_row = Adw.SwitchRow(title=_("Store hard links as links"), active=False)
        self.sfx_row = Adw.SwitchRow(title=_("Create self-extracting archive"),
                                     subtitle=_("A Linux program that unpacks itself when run"))
        self.sfx_row.connect("notify::active", lambda *_a: self._on_sfx_toggled())
        self.trash_row = Adw.SwitchRow(title=_("Move files to Trash after compression"), active=False)
        self.params_row = Adw.EntryRow(title=_("Extra 7-Zip parameters (e.g. -mf=off)"))
        self.params_row.connect("changed", lambda _r: self._validate())
        for row in (self.volume_row, self.volume_custom_row, self.update_row, self.path_row, self.symlinks_row,
                    self.hardlinks_row, self.sfx_row, self.trash_row, self.params_row):
            self.options_group.add(row)
        page.add(self.options_group)

        self.summary = Adw.PreferencesGroup()
        self.summary_label = Gtk.Label(label=_("Calculating size…"), xalign=0.5)
        self.summary_label.add_css_class("dim-label")
        self.summary_label.add_css_class("caption")
        self.summary.add(self.summary_label)
        page.add(self.summary)

        self._set_location(self.location)
        self._on_format_changed(initial=True)
        threading.Thread(target=self._measure, daemon=True).start()
        close_on_escape(self)
        self.connect("map", lambda _w: GLib.idle_add(self._focus_name))

    def _focus_name(self) -> bool:
        """Focus the name with only the part before the extension selected."""
        self.name_row.grab_focus()
        name = self.name_row.get_text()
        extension = jobs.CREATE_FORMATS[self.fmt][1]
        end = len(name) - len(extension) if name.endswith(extension) else len(name)
        self.name_row.select_region(0, end)
        return False

    # -- helpers ------------------------------------------------------------------
    @property
    def fmt(self) -> str:
        return self.formats[self.format_row.get_selected()]

    def _format_available(self, fmt: str) -> bool:
        if fmt == "7z":
            return self.tool_7z is not None
        if fmt == "wim":
            return self.tool_wim is not None
        return jobs.builtin_can_create(fmt) or (fmt == "zip" and self.tool_zip is not None)

    def _sevenzip_engine(self) -> bool:
        """True if 7-Zip itself will write this archive (more options apply)."""
        return self.fmt in ("7z", "wim") or (self.fmt == "zip" and self.tool_zip is not None)

    def _measure(self) -> None:
        total = 0
        files = 0
        for path in self.paths:
            if os.path.isdir(path) and not os.path.islink(path):
                for dirpath, _dirs, names in os.walk(path):
                    for name in names:
                        files += 1
                        try:
                            total += os.lstat(os.path.join(dirpath, name)).st_size
                        except OSError:
                            pass
            else:
                files += 1
                try:
                    total += os.lstat(path).st_size
                except OSError:
                    pass

        def done() -> bool:
            self.total_size = total
            self.summary_label.set_label(ngettext("{n} file · {size}", "{n} files · {size}", files).format(
                n=f"{files:,}", size=format_size(total)))
            self._refresh(keep_method=True)
            return False

        GLib.idle_add(done)

    def _set_location(self, path: str) -> None:
        self.location = path
        self.location_row.set_subtitle(display_folder(path))
        self.location_row.set_tooltip_text(path)
        self._validate()

    def _choose_location(self) -> None:
        dialog = Gtk.FileDialog(title=_("Save Archive In"), modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(self.location))

        def on_done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is not None and folder.get_path():
                self._set_location(folder.get_path())

        dialog.select_folder(self, None, on_done)

    def _archive_path(self) -> str:
        name = self.name_row.get_text().strip()
        if self.email:
            return name
        return os.path.join(self.location, name)

    # -- state updates ----------------------------------------------------------
    def _on_format_changed(self, initial: bool = False) -> None:
        fmt = self.fmt
        # Swap the extension in the name field.
        name = self.name_row.get_text()
        for other in sorted({v[1] for v in jobs.CREATE_FORMATS.values()} | {".run"}, key=len, reverse=True):
            if name.lower().endswith(other):
                name = name[: -len(other)]
                break
        self.name_row.set_text(name + jobs.CREATE_FORMATS[fmt][1])
        self.format_row.set_subtitle(FORMAT_INFO[fmt][1])
        self._refresh(initial=initial)

    def _on_sfx_toggled(self) -> None:
        name = self.name_row.get_text()
        if self.sfx_row.get_active() and name.endswith(".7z"):
            self.name_row.set_text(name[:-3] + ".run")
        elif not self.sfx_row.get_active() and name.endswith(".run"):
            self.name_row.set_text(name[:-4] + ".7z")
        self._validate()

    def _refresh(self, keep_method: bool = False, initial: bool = False) -> None:
        fmt = self.fmt
        sevenzip = self._sevenzip_engine()
        level = LEVELS[self.level_row.get_selected()][0]
        compressed = fmt not in ("tar",)
        self.level_row.set_visible(compressed)

        methods = METHODS.get(fmt, ()) if sevenzip else (BUILTIN_ZIP_METHODS if fmt == "zip" else ())
        current = None
        model = self.method_row.get_model()
        if keep_method and model is not None and model.get_n_items():
            current = model.get_string(self.method_row.get_selected())
        if not keep_method or tuple(model.get_string(i) for i in range(model.get_n_items())) != tuple(methods):
            self.method_row.set_model(Gtk.StringList.new(list(methods)))
            if current in methods:
                self.method_row.set_selected(methods.index(current))
        self.method_row.set_visible(bool(methods) and level != 0)
        method = methods[self.method_row.get_selected()] if methods else None

        lzma = sevenzip and fmt == "7z" and method in ("LZMA2", "LZMA")
        self.advanced.set_visible(sevenzip and fmt in ("7z", "zip") and level != 0)
        self.dict_row.set_visible(bool(lzma))
        self.word_row.set_visible(sevenzip and fmt in ("7z", "zip") and method in ("LZMA2", "LZMA", "Deflate",
                                                                                     "Deflate64"))
        self.solid_row.set_visible(sevenzip and fmt == "7z")
        self.threads_row.set_visible(sevenzip and fmt in ("7z", "zip"))
        self.memory_row.set_visible(bool(lzma))
        if lzma:
            selected = self.dict_row.get_selected()
            dictionary = DICTIONARIES[selected - 1] if selected > 0 else DEFAULT_DICT.get(level, 32 * MB)
            if self.total_size and selected == 0:
                # 7-Zip shrinks the dictionary to the data size.
                dictionary = min(dictionary, max(1 << max(self.total_size - 1, 1).bit_length(), 64 * KB))
            threads = self.threads_row.get_selected() or self.cpu_count
            comp, decomp = lzma_memory(dictionary, threads, level, method == "LZMA2")
            self.memory_row.set_subtitle(_("{compress} to compress · {decompress} to decompress").format(
                compress=format_size(comp), decompress=format_size(decomp)))
            available = _available_memory()
            if available and comp > available:
                self.memory_row.add_css_class("warning")
                self.memory_row.set_title(_("Memory usage — more than is available"))
            else:
                self.memory_row.remove_css_class("warning")
                self.memory_row.set_title(_("Memory usage"))

        can_encrypt = fmt in ("7z", "zip") and sevenzip
        self.encryption_group.set_sensitive(can_encrypt)
        if can_encrypt:
            self.encryption_group.set_description(None)
        elif fmt in ("7z", "zip"):
            self.encryption_group.set_description(_("Creating encrypted archives requires 7-Zip."))
        else:
            self.encryption_group.set_description(_("{format} archives cannot be encrypted.").format(
                format=FORMAT_INFO[fmt][0]))
        self.enc_method_row.set_visible(fmt == "zip")
        self.encrypt_names_row.set_visible(fmt == "7z")

        self.volume_row.set_visible(sevenzip)
        self.update_row.set_visible(sevenzip)
        self.path_row.set_visible(sevenzip)
        self.hardlinks_row.set_visible(sevenzip)
        self.sfx_row.set_visible(fmt == "7z" and self.tool_7z is not None and self.tool_7z.sfx_module is not None)
        if not self.sfx_row.get_visible() and self.sfx_row.get_active():
            self.sfx_row.set_active(False)
        self.params_row.set_visible(sevenzip)
        self._validate()

    def _volume_value(self) -> tuple[str | None, bool]:
        value = VOLUMES[self.volume_row.get_selected()][0]
        if value != "custom":
            self.volume_custom_row.set_visible(False)
            return value, True
        self.volume_custom_row.set_visible(True)
        parsed = parse_volume(self.volume_custom_row.get_text())
        return parsed, parsed is not None

    def _validate(self) -> None:
        if not hasattr(self, "params_row"):
            return
        ok = True
        fmt = self.fmt
        name = self.name_row.get_text().strip()
        bad_name = not name or "/" in name or name in (".", "..")
        _toggle_error(self.name_row, bad_name)
        ok &= not bad_name

        available = self._format_available(fmt)
        if not available:
            command = tool.install_command()
            self.banner.set_title(_("Install 7-Zip to create {format} archives.").format(format=FORMAT_INFO[fmt][0]))
            self.banner.set_button_label(_("Copy Command") if command else None)
            self.banner.set_revealed(True)
            ok = False
        else:
            self.banner.set_revealed(False)

        password = self.password_row.get_text()
        mismatch = bool(password) and password != self.confirm_row.get_text()
        _toggle_error(self.confirm_row, mismatch and bool(self.confirm_row.get_text()))
        if self.encryption_group.get_sensitive():
            ok &= not mismatch

        if self.volume_row.get_visible():
            _value, valid = self._volume_value()
            _toggle_error(self.volume_custom_row, not valid)
            ok &= valid
        params_ok = True
        if self.params_row.get_visible() and self.params_row.get_text().strip():
            try:
                tokens = shlex.split(self.params_row.get_text())
                params_ok = all(t.startswith("-") for t in tokens)
            except ValueError:
                params_ok = False
        _toggle_error(self.params_row, not params_ok)
        ok &= params_ok

        path = self._archive_path()
        exists = not self.email and os.path.exists(path)
        if exists:
            if fmt in ("7z", "zip", "wim"):
                self.name_row.set_tooltip_text(_("The archive exists: files will be added to it"))
            else:
                self.name_row.set_tooltip_text(_("The archive exists and will be replaced"))
        else:
            self.name_row.set_tooltip_text(None)
        self.update_row.set_sensitive(exists)
        self.create_button.set_sensitive(ok)

    def _on_banner(self, _banner) -> None:
        command = tool.install_command()
        if command:
            self.get_clipboard().set(command)
            self.toasts.add_toast(Adw.Toast(title=_("Copied “{command}”").format(command=command)))

    # -- create -----------------------------------------------------------------
    def _request(self) -> jobs.CreateRequest:
        fmt = self.fmt
        level = LEVELS[self.level_row.get_selected()][0]
        sevenzip = self._sevenzip_engine()
        methods = METHODS.get(fmt, ()) if sevenzip else (BUILTIN_ZIP_METHODS if fmt == "zip" else ())
        method = methods[self.method_row.get_selected()] if methods and self.method_row.get_visible() else None
        dictionary = None
        if self.dict_row.get_visible() and self.dict_row.get_selected() > 0:
            dictionary = _sevenzip_size(DICTIONARIES[self.dict_row.get_selected() - 1])
        word = None
        if self.word_row.get_visible() and self.word_row.get_selected() > 0:
            word = WORD_SIZES[self.word_row.get_selected() - 1]
        solid = None
        if self.solid_row.get_visible():
            index = self.solid_row.get_selected()
            if index == 1:
                solid = "off"
            elif index == len(SOLID_SIZES) + 2:
                solid = "on"
            elif index >= 2:
                solid = SOLID_SIZES[index - 2]
        threads = self.threads_row.get_selected() if self.threads_row.get_visible() else 0
        password = self.password_row.get_text() if self.encryption_group.get_sensitive() else ""
        volume, _ok = self._volume_value() if self.volume_row.get_visible() else (None, True)
        extra = shlex.split(self.params_row.get_text()) if self.params_row.get_visible() else []
        archive = self._archive_path()
        if self.email:
            archive = jobs.email_archive_path(self.paths, os.path.basename(archive))
        return jobs.CreateRequest(
            sources=self.paths,
            archive=archive,
            format=fmt,
            level=level,
            method=method,
            dictionary=dictionary,
            word_size=word,
            solid=solid,
            threads=threads or None,
            password=password or None,
            encrypt_names=self.encrypt_names_row.get_active() and fmt == "7z",
            zip_encryption="ZipCrypto" if self.enc_method_row.get_selected() == 1 else "AES256",
            volume_size=volume,
            update_mode=UPDATE_MODES[self.update_row.get_selected()][0] if self.update_row.get_sensitive() else "add",
            path_mode=PATH_MODES[self.path_row.get_selected()][0] if self.path_row.get_visible() else "relative",
            store_symlinks=self.symlinks_row.get_active(),
            store_hardlinks=self.hardlinks_row.get_active() and self.hardlinks_row.get_visible(),
            sfx=self.sfx_row.get_active() and self.sfx_row.get_visible(),
            trash_sources=self.trash_row.get_active(),
            extra_switches=extra,
        )

    def _remember(self, request: jobs.CreateRequest) -> None:
        self.settings.set("add.format", request.format)
        self.settings.set("add.level", request.level)
        self.settings.set("add.store_symlinks", request.store_symlinks)
        self.settings.set("add.update_mode", request.update_mode)
        self.settings.set("add.path_mode", request.path_mode)
        self.settings.set("add.encrypt_names", self.encrypt_names_row.get_active())
        self.settings.set("add.zip_encryption", request.zip_encryption)
        self.settings.set("add.threads", request.threads or 0)

    def _on_create(self, *_args) -> None:
        if not self.create_button.get_sensitive():
            return
        request = self._request()

        def go() -> None:
            from .actions import start_create

            self._remember(request)
            start_create(self.app, request, email=self.email)
            self.destroy()

        if request.trash_sources:
            dialog = alert(self, _("Move the Originals to the Trash?"),
                           ngettext("After the archive is created, the selected item will be moved to the Trash.",
                                    "After the archive is created, the {n} selected items will be moved to the "
                                    "Trash.", len(self.paths)).format(n=len(self.paths)))
            dialog.add_response("cancel", _("_Cancel"))
            dialog.add_response("go", _("_Create and Move to Trash"))
            dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, r: go() if r == "go" else None)
            present(dialog, self)
            return
        if not self.email and os.path.exists(request.archive) and request.format not in ("7z", "zip", "wim"):
            dialog = alert(self, _("Replace “{name}”?").format(name=os.path.basename(request.archive)),
                           _("An archive with this name already exists. Replacing it will overwrite its contents."))
            dialog.add_response("cancel", _("_Cancel"))
            dialog.add_response("replace", _("_Replace"))
            dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, r: go() if r == "replace" else None)
            present(dialog, self)
            return
        go()


def _toggle_error(widget: Gtk.Widget, error: bool) -> None:
    if error:
        widget.add_css_class("error")
    else:
        widget.remove_css_class("error")
