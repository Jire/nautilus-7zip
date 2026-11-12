# SPDX-License-Identifier: MIT
"""The helper application started by the Nautilus extension.

It is a single-instance GTK application: the first invocation becomes the
primary instance and later ones (another menu click) just forward their
command line to it, so every window lives in one process.

    nautilus-7zip open ARCHIVE
    nautilus-7zip extract (--dialog | --here | --to-folder | --dest DIR) -- ARCHIVE…
    nautilus-7zip test -- ARCHIVE…
    nautilus-7zip add [--dialog] [--email] [--format 7z|zip|tar.gz|…] -- FILE…
    nautilus-7zip hash [--algorithm ID|all] [--save] -- FILE…
    nautilus-7zip verify -- CHECKSUM-FILE
    nautilus-7zip preferences
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__, hashalgos, jobs  # noqa: E402
from .engines.base import OverwriteMode  # noqa: E402
from .i18n import _  # noqa: E402
from .settings import Settings  # noqa: E402

CSS = """
.tag {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 0.8em;
  font-weight: 700;
  background-color: alpha(currentColor, 0.12);
}
.hash-row .subtitle {
  font-family: monospace;
}
.operation-progress.paused trough progress {
  opacity: 0.45;
}
.details-view, .details-view text {
  background-color: transparent;
}
.browser-status {
  border-top: 1px solid alpha(currentColor, 0.12);
}
"""


COMMANDS = ("open", "extract", "test", "add", "hash", "verify", "preferences")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="nautilus-7zip", description=_("The full 7-Zip menu for GNOME Files."))
    parser.add_argument("--version", action="store_true", help=_("show the version and exit"))
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("open", help=_("browse an archive"))
    p.add_argument("files", nargs="+")

    p = sub.add_parser("extract", help=_("extract archives"))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dialog", action="store_true", help=_("show the Extract dialog"))
    mode.add_argument("--here", action="store_true", help=_("extract into the archive’s folder"))
    mode.add_argument("--to-folder", action="store_true", help=_("extract into a folder named after the archive"))
    mode.add_argument("--dest", metavar="DIR", help=_("extract into DIR"))
    p.add_argument("files", nargs="+")

    p = sub.add_parser("test", help=_("test archive integrity"))
    p.add_argument("files", nargs="+")

    p = sub.add_parser("add", help=_("compress files"))
    p.add_argument("--dialog", action="store_true", help=_("show the Add to Archive dialog"))
    p.add_argument("--email", action="store_true", help=_("attach the archive to a new email"))
    p.add_argument("--format", default="7z", choices=sorted(jobs.CREATE_FORMATS))
    p.add_argument("files", nargs="+")

    p = sub.add_parser("hash", help=_("calculate checksums"))
    p.add_argument("--algorithm", default="sha256", choices=[a.id for a in hashalgos.ALGORITHMS] + ["all"])
    p.add_argument("--save", action="store_true", help=_("save a .sha256 file instead of showing the results"))
    p.add_argument("files", nargs="+")

    p = sub.add_parser("verify", help=_("verify a checksum file"))
    p.add_argument("files", nargs=1)

    sub.add_parser("preferences", help=_("show the preferences"))
    return parser


def _print(command_line, text: str) -> None:
    """Print on the invoking terminal (print_literal needs GLib 2.80)."""
    if hasattr(command_line, "print_literal"):
        command_line.print_literal(text)
    else:
        sys.stdout.write(text)


def _printerr(command_line, text: str) -> None:
    if hasattr(command_line, "printerr_literal"):
        command_line.printerr_literal(text)
    else:
        sys.stderr.write(text)


class Application(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.HANDLES_OPEN)
        self.settings = Settings()
        self.set_option_context_parameter_string(_("[COMMAND] [FILE…]"))

    # -- lifecycle ------------------------------------------------------------
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(_("7-Zip for Files"))
        Gtk.Window.set_default_icon_name(APP_ID)
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)
        except AttributeError:  # GTK < 4.12
            provider.load_from_data(CSS, -1)
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def add(name, callback, parameter=None):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(parameter) if parameter else None)
            action.connect("activate", callback)
            self.add_action(action)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, self._on_terminate)

        add("about", lambda *_a: self._about())
        add("preferences", lambda *_a: self.show_preferences())
        add("quit", lambda *_a: self.quit())
        add("show-in-files", self._on_show_in_files, "as")
        add("open-path", self._on_open_path, "s")
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def _on_terminate(self) -> bool:
        """Cancel running jobs (so they clean up) before quitting on SIGTERM/SIGINT."""
        running = [w for w in self.get_windows() if getattr(w, "running", False)]
        for window in running:
            window.callbacks.cancel()
        deadline = GLib.get_monotonic_time() + 5_000_000

        def wait() -> bool:
            busy = any(getattr(w, "running", False) for w in self.get_windows())
            if busy and GLib.get_monotonic_time() < deadline:
                return True
            self.quit()
            return False

        GLib.timeout_add(100, wait)
        return GLib.SOURCE_CONTINUE

    def do_activate(self) -> None:
        self.show_preferences()

    def do_open(self, files, _n_files, _hint) -> None:
        from .ui.browser import open_archive

        for file in files:
            path = file.get_path()
            if path:
                open_archive(self, path)

    def do_command_line(self, command_line) -> int:
        args = command_line.get_arguments()[1:]
        cwd = command_line.get_cwd() or os.getcwd()
        if os.environ.get("NAUTILUS_7ZIP_DEBUG"):
            print("nautilus-7zip: command line", args, "in", cwd, file=sys.stderr, flush=True)
        if args and args[0] not in COMMANDS and not args[0].startswith("-"):
            args = ["open", *args]  # "nautilus-7zip FILE…" (Open With…, the .desktop file)
        try:
            options = build_parser().parse_args(args)
        except ValueError as error:
            _printerr(command_line, f"nautilus-7zip: {error}\n")
            return 2
        except SystemExit:  # --help
            _print(command_line, build_parser().format_help())
            return 0
        if options.version:
            _print(command_line, f"nautilus-7zip {__version__}\n")
            return 0
        if not options.command:
            self.activate()
            return 0
        if options.command == "preferences":
            self.show_preferences()
            return 0
        files = [os.path.abspath(os.path.join(cwd, f)) for f in options.files]
        missing = [f for f in files if not os.path.lexists(f)]
        if missing:
            _printerr(command_line, _("nautilus-7zip: no such file: {path}").format(path=missing[0]) + "\n")
            files = [f for f in files if f not in missing]
            if not files:
                return 1
        try:
            self.dispatch(options, files)
        except Exception as error:
            import traceback

            traceback.print_exc()
            _printerr(command_line, f"nautilus-7zip: {error}\n")
            return 1
        return 0

    # -- commands -------------------------------------------------------------
    def dispatch(self, options, files: list[str]) -> None:
        from .ui import actions

        command = options.command
        if command == "open":
            from .ui.browser import open_archive

            for path in files[:8]:
                open_archive(self, path)
        elif command == "extract":
            if options.dialog:
                self.open_extract_dialog(files)
                return
            request = jobs.ExtractRequest(
                archives=files,
                eliminate_root=bool(self.settings.get("eliminate_root_duplication", True)),
                overwrite=OverwriteMode.ASK,
            )
            if options.to_folder:
                request.mode = "folder"
            elif options.dest:
                request.mode = "custom"
                request.destination = os.path.abspath(options.dest)
            else:
                request.mode = "here"
                request.smart_here = bool(self.settings.get("smart_extract_here", False))
            actions.start_extract(self, request)
        elif command == "test":
            actions.start_test(self, files)
        elif command == "add":
            if options.dialog:
                from .ui.add_dialog import AddDialog

                AddDialog(self, files, email=options.email).present()
                return
            request = actions.default_create_request(self, files, options.format, email=options.email)
            actions.start_create(self, request, email=options.email)
        elif command == "hash":
            actions.start_hash(self, files, options.algorithm, save=options.save)
        elif command == "verify":
            actions.start_verify(self, files[0])

    def open_extract_dialog(self, archives: list[str]) -> None:
        from .ui.extract_dialog import ExtractDialog

        ExtractDialog(self, archives).present()

    def show_preferences(self) -> None:
        from .ui.preferences import PreferencesWindow

        for window in self.get_windows():
            if isinstance(window, PreferencesWindow):
                window.present()
                return
        PreferencesWindow(self).present()

    def _about(self) -> None:
        from .ui.preferences import show_about

        show_about(self, self.get_active_window())

    # -- notifications ----------------------------------------------------------
    def notify_finished(self, window, result, error) -> None:
        if not self.settings.get("notify_when_done", True):
            return
        title, body, paths = getattr(window, "notification", None) or (_("Done"), window.base_title, [])
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        if paths:
            target = GLib.Variant("as", paths)
            notification.set_default_action_and_target_value("app.show-in-files", target)
            notification.add_button_with_target_value(_("Show in Files"), "app.show-in-files", target)
        self.send_notification(None, notification)

    def _on_show_in_files(self, _action, parameter) -> None:
        from .ui.common import show_in_files

        show_in_files(list(parameter.unpack()), None)

    def _on_open_path(self, _action, parameter) -> None:
        from .ui.common import launch_file

        launch_file(parameter.get_string(), None)


def main(argv: list[str] | None = None) -> int:
    from .i18n import setup

    setup()
    argv = list(argv if argv is not None else sys.argv)
    # Answer --version/--help locally: no display or D-Bus session needed.
    if len(argv) > 1 and argv[1] == "--version":
        print(f"nautilus-7zip {__version__}")
        return 0
    if len(argv) > 1 and argv[1] in ("-h", "--help", "help"):
        build_parser().print_help()
        return 0
    if len(argv) > 2 and argv[1] in COMMANDS and argv[2] in ("-h", "--help"):
        try:
            build_parser().parse_args([argv[1], "--help"])
        except SystemExit:
            pass
        return 0
    app = Application()
    return app.run(argv)
