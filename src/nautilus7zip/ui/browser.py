# SPDX-License-Identifier: MIT
"""The archive browser behind "Open Archive" (a small 7-Zip File Manager)."""

from __future__ import annotations

import hashlib
import os

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .. import jobs, tool
from ..engines import engine_for
from ..engines.base import ArchiveError, ArchiveInfo, Cancelled, Entry, ErrorKind, OverwriteMode, PasswordBox
from ..i18n import _, ngettext
from ..settings import cache_dir
from . import prompts
from .common import (
    GtkCallbacks,
    content_icon,
    format_ratio,
    format_size,
    format_time,
    launch_file,
    make_spinner,
    run_in_thread,
    show_in_files,
)


class Node:
    __slots__ = ("_packed", "_size", "children", "entry", "is_dir", "name", "parent", "path")

    def __init__(self, name: str, path: str, is_dir: bool, parent: Node | None = None) -> None:
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.entry: Entry | None = None
        self.children: dict[str, Node] = {}
        self.parent = parent
        self._size: int | None = None
        self._packed: int | None = None

    @property
    def size(self) -> int | None:
        if not self.is_dir:
            return self.entry.size if self.entry else None
        if self._size is None:
            self._size = sum(c.size or 0 for c in self.children.values())
        return self._size

    @property
    def packed(self) -> int | None:
        if not self.is_dir:
            return self.entry.packed_size if self.entry else None
        if self._packed is None:
            values = [c.packed for c in self.children.values() if c.packed]
            self._packed = sum(values) if values else 0
        return self._packed or None

    @property
    def mtime(self) -> float | None:
        return self.entry.mtime if self.entry else None

    def count(self) -> tuple[int, int]:
        files = folders = 0
        for child in self.children.values():
            if child.is_dir:
                folders += 1
                f, d = child.count()
                files += f
                folders += d
            else:
                files += 1
        return files, folders

    def walk(self):
        for child in self.children.values():
            yield child
            if child.is_dir:
                yield from child.walk()


def build_tree(info: ArchiveInfo) -> Node:
    root = Node("", "", True)
    for entry in info.entries:
        parts = [p for p in entry.path.split("/") if p]
        if not parts:
            continue
        node = root
        for depth, part in enumerate(parts):
            last = depth == len(parts) - 1
            child = node.children.get(part)
            if child is None:
                child = Node(part, "/".join(parts[: depth + 1]), entry.is_dir or not last, node)
                node.children[part] = child
            elif last and not entry.is_dir and child.is_dir and not child.children:
                child.is_dir = False
            node = child
        node.entry = entry
    return root


class Item(GObject.Object):
    __gtype_name__ = "SevenZipBrowserItem"

    def __init__(self, node: Node, show_path: bool = False) -> None:
        super().__init__()
        self.node = node
        self.show_path = show_path


def _sort_key(column: str):
    def key(item: Item):
        node = item.node
        if column == "name":
            return node.name.casefold()
        if column == "size":
            return node.size or 0
        if column == "packed":
            return node.packed or 0
        if column == "modified":
            return node.mtime or 0
        if column == "method":
            return (node.entry.method or "") if node.entry else ""
        return node.name.casefold()

    return key


class BrowserWindow(Adw.ApplicationWindow):
    def __init__(self, app, archive: str) -> None:
        super().__init__(application=app, title=os.path.basename(archive))
        self.app = app
        self.archive = os.path.abspath(archive)
        self.password = PasswordBox()
        self.info: ArchiveInfo | None = None
        self.root: Node | None = None
        self.current: Node | None = None
        self.callbacks = GtkCallbacks(self)
        self.set_default_size(980, 640)
        self.set_size_request(360, 300)

        # Header -------------------------------------------------------------------
        header = Adw.HeaderBar()
        self.back_button = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Parent folder (Alt+Up)"),
                                      sensitive=False)
        self.back_button.connect("clicked", lambda _b: self.go_up())
        header.pack_start(self.back_button)
        self.window_title = Adw.WindowTitle(title=os.path.basename(archive), subtitle="")
        header.set_title_widget(self.window_title)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Extract All…"), "win.extract-all")
        section.append(_("Test Archive"), "win.test")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Show in Files"), "win.show-archive")
        section.append(_("Properties"), "win.properties")
        menu.append_section(None, section)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=_("Main menu"),
                                     primary=True)
        header.pack_end(menu_button)
        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text=_("Search (Ctrl+F)"))
        header.pack_end(self.search_button)
        self.extract_button = Gtk.Button(label=_("_Extract"), use_underline=True, sensitive=False)
        self.extract_button.add_css_class("suggested-action")
        self.extract_button.set_tooltip_text(_("Extract the selected items, or everything if nothing is selected"))
        self.extract_button.connect("clicked", lambda _b: self.extract_selection())
        header.pack_end(self.extract_button)

        for name, callback in (("extract-all", lambda *_a: self.extract_all()),
                               ("test", lambda *_a: self._test()),
                               ("show-archive", lambda *_a: show_in_files([self.archive], self)),
                               ("properties", lambda *_a: self.show_properties()),
                               ("open-selected", lambda *_a: self.open_selected()),
                               ("extract-selected", lambda *_a: self.extract_selection())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        app.set_accels_for_action("win.properties", ["<Alt>Return"])

        # Search -------------------------------------------------------------------
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search the whole archive"), hexpand=True)
        self.search_entry.connect("search-changed", lambda _e: self._refresh_list())
        self.search_bar = Gtk.SearchBar(show_close_button=False)
        clamp = Adw.Clamp(maximum_size=520)
        clamp.set_child(self.search_entry)
        self.search_bar.set_child(clamp)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.set_key_capture_widget(self)
        self.search_button.bind_property("active", self.search_bar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)

        # List ---------------------------------------------------------------------
        self.store = Gio.ListStore(item_type=Item)
        self.sorted = Gtk.SortListModel(model=self.store)
        self.selection = Gtk.MultiSelection(model=self.sorted)
        self.selection.connect("selection-changed", lambda *_a: self._update_status())
        self.view = Gtk.ColumnView(model=self.selection, show_row_separators=False, show_column_separators=False,
                                   reorderable=False)
        self.view.add_css_class("data-table")
        self.view.connect("activate", self._on_activate)
        self._columns: dict[str, Gtk.ColumnViewColumn] = {}
        self._add_column("name", _("Name"), self._setup_name, self._bind_name, expand=True)
        self._add_column("size", _("Size"), self._setup_right, lambda c, i: self._bind_text(
            c, format_size(i.node.size) if (i.node.size is not None) else ""), fixed=110)
        self._add_column("packed", _("Packed"), self._setup_right, lambda c, i: self._bind_text(
            c, format_size(i.node.packed) if i.node.packed else ""), fixed=110)
        self._add_column("modified", _("Modified"), self._setup_right, lambda c, i: self._bind_text(
            c, format_time(i.node.mtime) if i.node.mtime else ""), fixed=150)
        self._add_column("method", _("Method"), self._setup_left, lambda c, i: self._bind_text(
            c, self._method_text(i.node)), fixed=140)
        self.view.sort_by_column(self._columns["name"], Gtk.SortType.ASCENDING)
        self.sorted.set_sorter(self.view.get_sorter())

        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_click.connect("pressed", self._on_right_click)
        self.view.add_controller(right_click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.view)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        spinner = make_spinner()
        spinner.set_size_request(32, 32)
        loading.append(spinner)
        self.loading_label = Gtk.Label(label=_("Reading archive…"))
        self.loading_label.add_css_class("dim-label")
        loading.append(self.loading_label)
        self.stack.add_named(loading, "loading")
        self.stack.add_named(scroller, "list")
        self.empty_page = Adw.StatusPage(icon_name="folder-symbolic", title=_("Empty Folder"))
        self.stack.add_named(self.empty_page, "empty")
        self.error_page = Adw.StatusPage(icon_name="dialog-error-symbolic")
        self.stack.add_named(self.error_page, "error")

        self.status_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.status_label.add_css_class("caption")
        self.details_label = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.START)
        self.details_label.add_css_class("caption")
        self.details_label.add_css_class("dim-label")
        status = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        status.append(self.status_label)
        status.append(self.details_label)
        status.add_css_class("browser-status")

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.search_bar)
        toolbar.set_content(self.toasts)
        toolbar.add_bottom_bar(status)
        self.set_content(toolbar)
        self._load()

    # -- columns ------------------------------------------------------------------
    def _add_column(self, key, title, setup, bind, *, expand=False, fixed=None):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, cell: setup(cell))
        factory.connect("bind", lambda _f, cell: bind(cell, cell.get_item()))
        column = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand, resizable=True)
        if fixed:
            column.set_fixed_width(fixed)
        column.set_sorter(Gtk.CustomSorter.new(self._make_compare(key), None))
        self.view.append_column(column)
        self._columns[key] = column

    def _make_compare(self, key: str):
        getter = _sort_key(key)

        def compare(a, b, _data):
            # Folders always first, like Files.
            if a.node.is_dir != b.node.is_dir:
                return -1 if a.node.is_dir else 1
            ka, kb = getter(a), getter(b)
            if ka == kb:
                ka, kb = a.node.name.casefold(), b.node.name.casefold()
            return (ka > kb) - (ka < kb)

        return compare

    @staticmethod
    def _setup_name(cell) -> None:
        box = Gtk.Box(spacing=10)
        image = Gtk.Image(pixel_size=16)
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
        lock = Gtk.Image(icon_name="system-lock-screen-symbolic", visible=False, tooltip_text=_("Encrypted"))
        lock.add_css_class("dim-label")
        box.append(image)
        box.append(label)
        box.append(lock)
        cell.set_child(box)

    def _bind_name(self, cell, item: Item) -> None:
        box = cell.get_child()
        image = box.get_first_child()
        label = image.get_next_sibling()
        lock = label.get_next_sibling()
        node = item.node
        image.set_from_gicon(content_icon(node.name, is_dir=node.is_dir, symbolic=True))
        label.set_label(node.path if item.show_path else node.name)
        lock.set_visible(bool(node.entry and node.entry.encrypted))
        box.item = item  # for the context menu

    @staticmethod
    def _setup_right(cell) -> None:
        label = Gtk.Label(xalign=1)
        label.add_css_class("numeric")
        label.add_css_class("dim-label")
        cell.set_child(label)

    @staticmethod
    def _setup_left(cell) -> None:
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        label.add_css_class("dim-label")
        cell.set_child(label)

    @staticmethod
    def _bind_text(cell, text: str) -> None:
        cell.get_child().set_label(text)

    @staticmethod
    def _method_text(node: Node) -> str:
        if node.is_dir or node.entry is None:
            return ""
        return node.entry.method or ""

    # -- loading -----------------------------------------------------------------
    def prompt_password(self, archive_name: str, retry: bool, done) -> None:
        prompts.ask_password(self, archive_name, retry, done)

    def prompt_overwrite(self, existing, incoming, done) -> None:
        prompts.ask_overwrite(self, existing, incoming, done)

    def _load(self) -> None:
        self.stack.set_visible_child_name("loading")

        def job():
            engine = engine_for(self.archive, self.app.settings)
            return engine.list(self.archive, self.callbacks, self.password)

        run_in_thread(job, self._on_loaded)

    def _on_loaded(self, info, error) -> None:
        if error is not None:
            if isinstance(error, Cancelled):
                self.close()
                return
            message = error.message if isinstance(error, ArchiveError) else str(error)
            title = prompts.error_title(error.kind if isinstance(error, ArchiveError) else None)
            self.error_page.set_title(title)
            self.error_page.set_description(GLib.markup_escape_text(message))
            if isinstance(error, ArchiveError) and error.kind in (ErrorKind.NEEDS_7ZIP, ErrorKind.NEEDS_RAR):
                command = tool.install_command(rar=error.kind is ErrorKind.NEEDS_RAR)
                if command:
                    button = Gtk.Button(label=_("Copy Install Command"), halign=Gtk.Align.CENTER)
                    button.add_css_class("pill")
                    button.add_css_class("suggested-action")
                    button.connect("clicked", lambda b: (b.get_clipboard().set(command),
                                                         b.set_label(_("Copied"))))
                    self.error_page.set_child(button)
            self.stack.set_visible_child_name("error")
            return
        self.info = info
        self.root = build_tree(info)
        self.extract_button.set_sensitive(True)
        details = [info.type.upper() if info.type else ""]
        if info.method:
            details.append(info.method)
        if info.solid:
            details.append(_("Solid"))
        if info.encrypted:
            details.append(_("Encrypted"))
        self.details_label.set_label(" · ".join(d for d in details if d))
        self.navigate(self.root)

    # -- navigation ---------------------------------------------------------------
    def navigate(self, node: Node) -> None:
        self.current = node
        if self.search_bar.get_search_mode():
            self.search_entry.set_text("")
        self.window_title.set_subtitle("/" + node.path if node.path else _("Top level"))
        self.back_button.set_sensitive(node.parent is not None)
        self._refresh_list()

    def go_up(self) -> None:
        if self.current is not None and self.current.parent is not None:
            previous = self.current
            self.navigate(self.current.parent)
            for index in range(self.sorted.get_n_items()):
                if self.sorted.get_item(index).node is previous:
                    self.selection.select_item(index, True)
                    self.view.scroll_to(index, None, Gtk.ListScrollFlags.FOCUS, None)
                    break

    def _refresh_list(self) -> None:
        if self.root is None or self.current is None:
            return
        query = self.search_entry.get_text().strip().casefold()
        if query:
            nodes = [n for n in self.root.walk() if query in n.name.casefold()]
            items = [Item(n, show_path=True) for n in nodes[:5000]]
        else:
            items = [Item(n) for n in self.current.children.values()]
        self.store.splice(0, self.store.get_n_items(), items)
        if not items:
            if query:
                self.empty_page.set_icon_name("edit-find-symbolic")
                self.empty_page.set_title(_("No Results Found"))
                self.empty_page.set_description(_("Try a different search."))
            else:
                self.empty_page.set_icon_name("folder-symbolic")
                self.empty_page.set_title(_("Empty Folder"))
                self.empty_page.set_description(None)
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("list")
        self._update_status()

    def _selected_nodes(self) -> list[Node]:
        nodes = []
        bitset = self.selection.get_selection()
        for i in range(bitset.get_size()):
            nodes.append(self.sorted.get_item(bitset.get_nth(i)).node)
        return nodes

    def _update_status(self) -> None:
        count = self.sorted.get_n_items()
        selected = self._selected_nodes()
        if selected:
            size = sum(n.size or 0 for n in selected)
            if len(selected) == 1:
                text = _("“{name}” selected ({size})").format(name=selected[0].name, size=format_size(size))
            else:
                text = ngettext("{n} item selected ({size})", "{n} items selected ({size})",
                                len(selected)).format(n=len(selected), size=format_size(size))
        else:
            text = ngettext("{n} item", "{n} items", count).format(n=count)
            if self.current is not None and self.current is self.root and self.info is not None:
                text += " · " + _("{size} unpacked").format(size=format_size(self.info.total_size))
        self.status_label.set_label(text)

    def _on_activate(self, _view, position: int) -> None:
        node = self.sorted.get_item(position).node
        if node.is_dir:
            self.navigate(node)
        else:
            self.open_node(node)

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        alt = state & Gdk.ModifierType.ALT_MASK
        if (keyval == Gdk.KEY_BackSpace and not self.search_bar.get_search_mode()) or (alt and keyval == Gdk.KEY_Up):
            self.go_up()
            return True
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_button.set_active(True)
            return True
        return False

    def _on_right_click(self, gesture, _n, x, y) -> None:
        widget = self.view.pick(x, y, Gtk.PickFlags.DEFAULT)
        item = None
        while widget is not None and item is None:
            item = getattr(widget, "item", None)
            widget = widget.get_parent()
        if item is not None:
            position = next((i for i in range(self.sorted.get_n_items()) if self.sorted.get_item(i) is item), None)
            if position is not None and not self.selection.is_selected(position):
                self.selection.select_item(position, True)
        menu = Gio.Menu()
        menu.append(_("Open"), "win.open-selected")
        menu.append(_("Extract…"), "win.extract-selected")
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self.view)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    # -- actions ------------------------------------------------------------------
    def open_selected(self) -> None:
        nodes = self._selected_nodes()
        if len(nodes) == 1 and nodes[0].is_dir:
            self.navigate(nodes[0])
            return
        for node in nodes[:10]:
            if not node.is_dir:
                self.open_node(node)

    def open_node(self, node: Node) -> None:
        from .progress import OperationWindow

        digest = hashlib.sha1(self.archive.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        destination = os.path.join(cache_dir(), "open", digest)
        os.makedirs(destination, exist_ok=True)
        parent = node.path.rsplit("/", 1)[0] if "/" in node.path else ""
        request = jobs.ExtractRequest([self.archive], mode="custom", destination=destination,
                                      overwrite=OverwriteMode.OVERWRITE, members=[node.path], strip_prefix=parent,
                                      password_box=self.password)
        target = os.path.join(destination, node.name)

        def job(cb):
            return jobs.extract(request, cb, self.app.settings)

        def finished(window, _outcomes, error):
            if error is not None:
                message = error.message if isinstance(error, ArchiveError) else str(error)
                window.show_status(icon_name="dialog-error-symbolic", title=_("Could Not Open File"),
                                   description=message, style="error")
                return
            window.done_quietly()
            if os.path.exists(target):
                launch_file(target, self)

        OperationWindow(self.app, title=_("Opening “{name}”").format(name=node.name), verb=_("Opening"),
                        subtitle=os.path.basename(self.archive), icon=content_icon(node.name), job=job,
                        finished=finished, show_delay=600)

    def extract_selection(self) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            self.extract_all()
            return
        from .extract_dialog import ExtractDialog

        prefix = self.current.path if self.current is not None and not self.search_entry.get_text() else ""
        if self.search_entry.get_text():
            parents = {n.path.rsplit("/", 1)[0] if "/" in n.path else "" for n in nodes}
            prefix = parents.pop() if len(parents) == 1 else ""
        dialog = ExtractDialog(self.app, [self.archive], members=[n.path for n in nodes], strip_prefix=prefix,
                               subfolder=False)
        if self.password.value:
            dialog.password_row.set_text(self.password.value)
        dialog.set_transient_for(self)
        dialog.present()

    def extract_all(self) -> None:
        from .extract_dialog import ExtractDialog

        dialog = ExtractDialog(self.app, [self.archive])
        if self.password.value:
            dialog.password_row.set_text(self.password.value)
        dialog.set_transient_for(self)
        dialog.present()

    def _test(self) -> None:
        from .actions import start_test

        start_test(self.app, [self.archive])

    def show_properties(self) -> None:
        if self.info is None:
            return
        info = self.info
        dialog = Adw.PreferencesDialog(title=_("Properties"))
        dialog.set_content_width(520)
        page = Adw.PreferencesPage()
        general = Adw.PreferencesGroup()
        files, folders = self.root.count() if self.root else (info.files, info.folders)
        physical = info.physical_size or os.path.getsize(self.archive)
        rows = [
            (_("Name"), os.path.basename(self.archive)),
            (_("Location"), os.path.dirname(self.archive)),
            (_("Type"), info.type.upper() if info.type else "—"),
            (_("Size on disk"), format_size(physical)),
            (_("Unpacked size"), format_size(info.total_size)),
            (_("Compression ratio"), format_ratio(physical, info.total_size)),
            (_("Contents"), _("{files} · {folders}").format(
                files=ngettext("{n} file", "{n} files", files).format(n=f"{files:,}"),
                folders=ngettext("{n} folder", "{n} folders", folders).format(n=f"{folders:,}"))),
        ]
        if info.method:
            rows.append((_("Method"), info.method))
        if info.solid is not None:
            rows.append((_("Solid"), _("Yes") if info.solid else _("No")))
        if info.blocks:
            rows.append((_("Blocks"), str(info.blocks)))
        if info.headers_size:
            rows.append((_("Headers size"), format_size(info.headers_size)))
        if info.volumes:
            rows.append((_("Volumes"), str(info.volumes)))
        rows.append((_("Encrypted"), _("Contents and file names") if info.headers_encrypted else
                     (_("Contents") if info.encrypted else _("No"))))
        rows.append((_("Opened with"), info.engine or "—"))
        for title, value in rows:
            row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True, use_markup=False)
            row.add_css_class("property")
            general.add(row)
        page.add(general)
        if info.comment:
            group = Adw.PreferencesGroup(title=_("Comment"))
            label = Gtk.Label(label=info.comment, wrap=True, selectable=True, xalign=0)
            label.add_css_class("card")
            label.set_margin_top(6)
            group.add(label)
            page.add(group)
        if info.warnings:
            group = Adw.PreferencesGroup(title=_("Warnings"))
            for warning in info.warnings[:20]:
                group.add(Adw.ActionRow(title=warning, use_markup=False))
            page.add(group)
        dialog.add(page)
        dialog.present(self)


def open_archive(app, archive: str) -> None:
    """Honour the "Open archives with" preference."""
    choice = app.settings.get("open_with", "builtin")
    if choice == "7zfm":
        import shutil

        exe = shutil.which("7zFM")
        if exe:
            Gio.Subprocess.new([exe, archive], Gio.SubprocessFlags.NONE)
            return
    if choice == "default":
        content_type, _uncertain = Gio.content_type_guess(archive, None)
        info = Gio.AppInfo.get_default_for_type(content_type, False)
        from .. import APP_ID

        if info is not None and info.get_id() != APP_ID + ".desktop":
            info.launch([Gio.File.new_for_path(archive)], None)
            return
    BrowserWindow(app, archive).present()
