# SPDX-License-Identifier: MIT
"""What each menu item does: start the job, then present the outcome."""

from __future__ import annotations

import os

from gi.repository import Gio

from .. import formats, jobs, tool
from ..engines.base import ArchiveError, ErrorKind
from ..i18n import _, ngettext
from .common import content_icon, copy_to_clipboard, display_folder, format_ratio, format_size, show_in_files
from .progress import OperationWindow
from .prompts import error_title


def _archive_icon(path: str) -> Gio.Icon:
    return content_icon(path)


def _error_page(window: OperationWindow, error: ArchiveError, *, retry=None, elsewhere=None) -> None:
    buttons = []
    rows = []
    description = error.message
    icon = "dialog-error-symbolic"
    if error.kind in (ErrorKind.NEEDS_7ZIP, ErrorKind.NEEDS_RAR):
        icon = "package-x-generic-symbolic"
        rar = error.kind is ErrorKind.NEEDS_RAR
        command = tool.install_command(rar=rar)
        if rar:
            description += "\n\n" + _("Install the RAR plugin for 7-Zip, then try again.")
        else:
            description += "\n\n" + _("ZIP and TAR archives work without it, but 7z, RAR, ISO and dozens of other "
                                      "formats need 7-Zip.")
        if command:
            rows.append((_("Install with"), command))
            buttons.append((_("Copy _Command"), lambda: (copy_to_clipboard(window, command), _("Copied"))[1],
                            "suggested-action"))
    elif error.kind is ErrorKind.WRONG_PASSWORD and retry is not None:
        icon = "dialog-password-symbolic"
        buttons.append((_("_Try Again"), retry, "suggested-action"))
    elif error.kind in (ErrorKind.PERMISSION, ErrorKind.DISK_FULL) and elsewhere is not None:
        buttons.append((_("Extract _Elsewhere…"), elsewhere, "suggested-action"))
    if error.archive:
        rows.insert(0, (_("Archive"), os.path.basename(error.archive)))
    needs_install = error.kind in (ErrorKind.NEEDS_7ZIP, ErrorKind.NEEDS_RAR)
    window.notification = (error_title(error.kind), error.message, [])
    window.show_status(icon_name=icon, title=error_title(error.kind), description=description,
                       style=None if needs_install else "error", rows=rows, details=error.details or None,
                       buttons=buttons, monospace_rows={_("Install with")})


def _unexpected(window: OperationWindow, error: BaseException) -> None:
    import traceback

    details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    window.show_status(icon_name="dialog-error-symbolic", title=_("Something Went Wrong"),
                       description=str(error) or type(error).__name__, style="error", details=details)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def start_extract(app, request: jobs.ExtractRequest) -> OperationWindow:
    archives = request.archives
    first = archives[0]
    count = len(archives)
    if count == 1:
        title = _("Extracting “{name}”").format(name=os.path.basename(first))
    else:
        title = ngettext("Extracting {n} archive", "Extracting {n} archives", count).format(n=count)
    if request.mode == "custom" and request.destination:
        where = request.destination
    else:
        where = os.path.dirname(os.path.abspath(first))
    if request.mode == "folder" and count == 1:
        where = os.path.join(where, formats.archive_stem(os.path.basename(first)))
    subtitle = _("To “{folder}”").format(folder=display_folder(where)) if count == 1 or request.mode != "folder" \
        else _("Each into its own folder")
    settings = app.settings

    def job(cb):
        return jobs.extract(request, cb, settings)

    def finished(window: OperationWindow, outcomes, error) -> None:
        if error is not None:
            if isinstance(error, ArchiveError):
                def retry():
                    window.destroy()
                    start_extract(app, request)

                def elsewhere():
                    window.destroy()
                    app.open_extract_dialog(request.archives)

                _error_page(window, error, retry=retry, elsewhere=elsewhere)
            else:
                _unexpected(window, error)
            return
        outputs = [p for o in outcomes for p in o.report.outputs]
        failed = [o for o in outcomes if not o.report.ok]
        warnings = [w for o in outcomes for w in o.report.warnings]
        window.notification = (
            _("Extraction finished") if not failed else _("Extraction finished with errors"),
            _("“{name}” was extracted.").format(name=os.path.basename(first)) if count == 1 else
            ngettext("{n} archive was extracted.", "{n} archives were extracted.", count).format(n=count),
            outputs,
        )
        if not failed and not warnings:
            window.done_quietly()
            return
        rows = []
        for outcome in outcomes:
            status = _("OK") if outcome.report.ok else outcome.report.errors[0]
            rows.append((os.path.basename(outcome.archive), status))
        title = _("Extracted With Errors") if failed else _("Extracted With Warnings")
        description = _("Some files could not be extracted.") if failed else \
            _("The archive was extracted, but 7-Zip reported some problems.")
        buttons = [(_("Show in _Files"), lambda: (show_in_files(outputs, window), window.close()),
                    "suggested-action")] if outputs else []
        window.show_status(icon_name="dialog-warning-symbolic", title=title, description=description,
                           style="warning", rows=rows, details="\n".join(warnings) or None, buttons=buttons)

    return OperationWindow(app, title=title, verb=_("Extracting"), subtitle=subtitle, icon=_archive_icon(first),
                           job=job, finished=finished)


# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------


def start_test(app, archives: list[str]) -> OperationWindow:
    count = len(archives)
    title = _("Testing “{name}”").format(name=os.path.basename(archives[0])) if count == 1 else \
        ngettext("Testing {n} archive", "Testing {n} archives", count).format(n=count)

    def job(cb):
        return jobs.test(archives, cb, app.settings)

    def finished(window: OperationWindow, outcomes, error) -> None:
        if error is not None:
            if isinstance(error, ArchiveError):
                _error_page(window, error, retry=lambda: (window.destroy(), start_test(app, archives)))
            else:
                _unexpected(window, error)
            return
        failed = [o for o in outcomes if not o.report.ok]
        files = sum(o.report.files for o in outcomes)
        folders = sum(o.report.folders for o in outcomes)
        size = sum(o.report.size for o in outcomes)
        packed = sum((o.report.packed or 0) for o in outcomes) or None
        if packed is None:
            packed = sum(os.path.getsize(o.archive) for o in outcomes if os.path.exists(o.archive)) or None
        window.notification = (
            _("Test finished") if not failed else _("Errors found"),
            _("No errors were found.") if not failed else
            ngettext("{n} archive is damaged.", "{n} archives are damaged.", len(failed)).format(n=len(failed)),
            [],
        )
        if not failed:
            rows = []
            if count > 1:
                rows.append((_("Archives"), str(count)))
            contents = ngettext("{n} file", "{n} files", files).format(n=f"{files:,}")
            if folders:
                contents += " · " + ngettext("{n} folder", "{n} folders", folders).format(n=f"{folders:,}")
            rows += [
                (_("Contents"), contents),
                (_("Size"), format_size(size)),
                (_("Packed size"), _("{size} ({ratio} of the original)").format(
                    size=format_size(packed), ratio=format_ratio(packed, size)) if packed and size else
                 format_size(packed)),
            ]
            description = (_("“{name}” passed the integrity test.").format(name=os.path.basename(archives[0]))
                           if count == 1 else _("All archives passed the integrity test."))
            warnings = [w for o in outcomes for w in o.report.warnings]
            window.show_status(icon_name="object-select-symbolic", title=_("No Errors Found"), description=description,
                               style="success", rows=rows, details="\n".join(warnings) or None,
                               header=_("Test Result"))
            return
        rows = []
        for outcome in outcomes:
            rows.append((os.path.basename(outcome.archive),
                         _("OK") if outcome.report.ok else outcome.report.errors[0]))
        details = "\n".join(w for o in failed for w in o.report.warnings) or None
        kinds = {o.report.error_kind for o in failed}
        buttons = []
        if kinds & {ErrorKind.NEEDS_7ZIP, ErrorKind.NEEDS_RAR}:
            command = tool.install_command(rar=ErrorKind.NEEDS_RAR in kinds)
            if command:
                rows.append((_("Install with"), command))
                buttons.append((_("Copy _Command"), lambda: (copy_to_clipboard(window, command), _("Copied"))[1],
                                "suggested-action"))
        title = error_title(next(iter(kinds))) if count == 1 else _("Errors Found")
        needs_install = bool(kinds & {ErrorKind.NEEDS_7ZIP, ErrorKind.NEEDS_RAR})
        window.show_status(icon_name="dialog-error-symbolic", title=title,
                           description=failed[0].report.errors[0] if count == 1 else
                           ngettext("{n} archive has errors.", "{n} archives have errors.",
                                    len(failed)).format(n=len(failed)),
                           style=None if needs_install else "error", rows=rows, details=details, buttons=buttons,
                           monospace_rows={_("Install with")}, header=_("Test Result"))

    return OperationWindow(app, title=title, verb=_("Testing"), subtitle=display_folder(os.path.dirname(archives[0])),
                           icon=_archive_icon(archives[0]), job=job, finished=finished, show_delay=0)


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------


def start_create(app, request: jobs.CreateRequest, *, email: bool = False) -> OperationWindow:
    name = os.path.basename(request.archive)
    exists = os.path.exists(request.archive)
    title = (_("Updating “{name}”") if exists and request.format in ("7z", "zip", "wim") else
             _("Creating “{name}”")).format(name=name)
    settings = app.settings

    def job(cb):
        report = jobs.create(request, cb, settings)
        sent, client = False, None
        if email:
            client, attaches = jobs.mail_client()
            if attaches:
                cb.stage(_("Opening {app}…").format(app=client))
                sent = jobs.send_email((report.outputs or [request.archive])[0])
        return report, sent, client

    def finished(window: OperationWindow, result, error) -> None:
        if error is not None:
            if isinstance(error, ArchiveError):
                _error_page(window, error)
            else:
                _unexpected(window, error)
            return
        report, sent, client = result
        outputs = report.outputs or [request.archive]
        window.notification = (_("Archive created"), _("“{name}” is ready.").format(name=name),
                               [] if email else outputs)
        if email:
            if sent:
                window.done_quietly()
                return
            if client:
                description = _("Your email opens in {app}, which can’t attach files by itself. Drag the archive "
                                "into your message, or attach it from its folder.").format(app=client)
            else:
                description = _("No email application is set up. Attach the archive to a message yourself.")
            buttons = [(_("Show in _Files"), lambda: show_in_files(outputs, window), "suggested-action")]
            if client:
                buttons.append((_("_Write Email"), lambda: Gio.AppInfo.launch_default_for_uri("mailto:", None), None))
            window.show_status(icon_name="mail-unread-symbolic", title=_("Ready to Send"), description=description,
                               rows=[(_("Archive"), outputs[0])], buttons=buttons, header=_("Compress and Email"))
            return
        if report.warnings:
            window.show_status(icon_name="dialog-warning-symbolic", title=_("Created With Warnings"),
                               description=_("The archive was created, but some items were skipped."),
                               style="warning", details="\n".join(report.warnings),
                               buttons=[(_("Show in _Files"), lambda: (show_in_files(outputs, window),
                                                                       window.close()), "suggested-action")])
            return
        window.done_quietly()

    return OperationWindow(app, title=title, verb=_("Compressing"),
                           subtitle=_("In “{folder}”").format(folder=display_folder(os.path.dirname(request.archive)))
                           if not email else _("Preparing an email attachment"),
                           icon=_archive_icon(request.archive), job=job, finished=finished, compressing=True)


def default_create_request(app, paths: list[str], fmt: str, *, email: bool = False) -> jobs.CreateRequest:
    """The request behind "Add to “name.7z”" / "Add to “name.zip”"."""
    filename = formats.archive_name_for(paths) + jobs.CREATE_FORMATS[fmt][1]
    if email:
        archive = jobs.email_archive_path(paths, filename)
    else:
        archive = os.path.join(formats.common_parent(paths), filename)
    remembered = app.settings.get("add", {})
    return jobs.CreateRequest(
        sources=paths,
        archive=archive,
        format=fmt,
        level=5,
        store_symlinks=bool(remembered.get("store_symlinks", True)),
    )


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def resolve_algorithms(algorithm: str, settings=None) -> list[str]:
    from .. import hashalgos

    if algorithm != "all":
        return [algorithm]
    override = settings.get("sevenzip_path") if settings is not None else None
    full = tool.best_tool(hashers=[a.sevenzip for a in hashalgos.ALGORITHMS], override=override)
    if full is not None:
        return [a.id for a in hashalgos.ALGORITHMS]
    return [a.id for a in hashalgos.ALGORITHMS if a.builtin]


def start_hash(app, paths: list[str], algorithm: str, *, save: bool = False) -> OperationWindow:
    from .. import hashalgos

    algorithms = resolve_algorithms(algorithm, app.settings)
    label = hashalgos.BY_ID[algorithms[0]].label if len(algorithms) == 1 else _("checksums")
    title = _("Calculating {algorithm}").format(algorithm=label)

    def job(cb):
        return jobs.compute_checksums(paths, algorithms, cb, app.settings)

    def finished(window: OperationWindow, result, error) -> None:
        if error is not None:
            if isinstance(error, ArchiveError):
                _error_page(window, error)
            else:
                _unexpected(window, error)
            return
        if save:
            if len(paths) == 1 and not os.path.isdir(paths[0]):
                target = paths[0] + ".sha256"
            else:
                target = os.path.join(formats.common_parent(paths), formats.archive_name_for(paths) + ".sha256")
            try:
                jobs.write_checksum_file(result, "sha256", target)
            except OSError as exc:
                window.show_status(icon_name="dialog-error-symbolic", title=_("Could Not Save Checksums"),
                                   description=exc.strerror or str(exc), style="error")
                return
            window.notification = (_("Checksums saved"), os.path.basename(target), [target])
            window.done_quietly()
            return
        from .checksums import ChecksumWindow

        ChecksumWindow(app, result, paths).present()
        window.done_quietly()

    return OperationWindow(app, title=title, verb=_("Calculating"), subtitle=display_folder(
        formats.common_parent(paths)), icon=Gio.ThemedIcon.new("auth-fingerprint-symbolic"), job=job, finished=finished)


def start_verify(app, checksum_file: str) -> OperationWindow:
    title = _("Verifying “{name}”").format(name=os.path.basename(checksum_file))

    def job(cb):
        return jobs.verify_checksums(checksum_file, cb)

    def finished(window: OperationWindow, rows, error) -> None:
        if error is not None:
            if isinstance(error, ArchiveError):
                _error_page(window, error)
            else:
                _unexpected(window, error)
            return
        from .checksums import VerifyWindow

        VerifyWindow(app, checksum_file, rows).present()
        window.done_quietly()

    return OperationWindow(app, title=title, verb=_("Verifying"),
                           subtitle=display_folder(os.path.dirname(checksum_file)),
                           icon=Gio.ThemedIcon.new("auth-fingerprint-symbolic"), job=job, finished=finished)
