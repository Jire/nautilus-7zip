# SPDX-License-Identifier: MIT
"""The progress window, modelled on 7-Zip's (elapsed, remaining, speed, …)."""

from __future__ import annotations

import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..engines.base import Cancelled
from ..i18n import _
from . import prompts
from .common import GtkCallbacks, copy_to_clipboard, format_duration, format_ratio, format_size, run_in_thread


class _Stat(Gtk.Box):
    def __init__(self, caption: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=caption, xalign=0)
        title.add_css_class("caption")
        title.add_css_class("dim-label")
        self.value = Gtk.Label(label="—", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.value.add_css_class("numeric")
        self.append(title)
        self.append(self.value)

    def set(self, text: str) -> None:
        if self.value.get_label() != text:
            self.value.set_label(text)


class OperationWindow(Adw.ApplicationWindow):
    """Runs ``job(callbacks)`` in a thread and shows its progress.

    ``finished(window, result, error)`` is called on the main loop and decides
    what to show next (usually: close quietly, or a result/error page).
    """

    def __init__(self, app: Adw.Application, *, title: str, verb: str, subtitle: str, icon: Gio.Icon, job,
                 finished, show_delay: int = 350, compressing: bool = False) -> None:
        super().__init__(application=app, title=title)
        self.set_default_size(520, -1)
        self.set_resizable(False)
        self.add_css_class("operation-window")
        self.verb = verb
        self.base_title = title
        self.job = job
        self.finished_cb = finished
        self.compressing = compressing
        self.callbacks = GtkCallbacks(self)
        self.running = True
        self.background = False
        self.result_shown = False
        self._started = time.monotonic()
        self._last_fraction: float | None = None
        self._speed_samples: list[tuple[float, int]] = []

        self.header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=verb, subtitle="")
        self.header.set_title_widget(self.window_title)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False,
                               interpolate_size=True)
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18, margin_bottom=24,
                       margin_start=24, margin_end=24)

        head = Gtk.Box(spacing=14)
        image = Gtk.Image.new_from_gicon(icon)
        image.set_pixel_size(48)
        head.append(image)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        self.title_label = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.title_label.add_css_class("heading")
        self.subtitle_label = Gtk.Label(label=subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.subtitle_label.add_css_class("dim-label")
        labels.append(self.title_label)
        labels.append(self.subtitle_label)
        head.append(labels)
        page.append(head)

        self.bar = Gtk.ProgressBar()
        self.bar.add_css_class("operation-progress")
        page.append(self.bar)

        line = Gtk.Box(spacing=12)
        self.current_label = Gtk.Label(label=_("Starting…"), xalign=0, hexpand=True,
                                       ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.current_label.add_css_class("caption")
        self.current_label.add_css_class("dim-label")
        self.percent_label = Gtk.Label(label="")
        self.percent_label.add_css_class("caption-heading")
        self.percent_label.add_css_class("numeric")
        line.append(self.current_label)
        line.append(self.percent_label)
        page.append(line)

        # Two columns, like 7-Zip's own progress window.
        grid = Gtk.Grid(column_spacing=24, row_spacing=12, column_homogeneous=True, margin_top=4)
        self.stat_elapsed = _Stat(_("Elapsed"))
        self.stat_remaining = _Stat(_("Remaining"))
        self.stat_processed = _Stat(_("Processed"))
        self.stat_speed = _Stat(_("Speed"))
        self.stat_files = _Stat(_("Files"))
        self.stat_ratio = _Stat(_("Compression ratio") if compressing else _("Archive size"))
        for index, stat in enumerate((self.stat_elapsed, self.stat_remaining, self.stat_processed,
                                      self.stat_speed, self.stat_files, self.stat_ratio)):
            grid.attach(stat, index % 2, index // 2, 1, 1)
        self.stat_ratio.set_visible(False)  # shown once there is something meaningful to say
        page.append(grid)

        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.END, margin_top=6)
        self.pause_button = Gtk.Button(label=_("_Pause"), use_underline=True)
        self.pause_button.set_tooltip_text(_("Pause or resume the operation"))
        self.pause_button.connect("clicked", self._on_pause)
        self.background_button = Gtk.Button(label=_("_Background"), use_underline=True)
        self.background_button.set_tooltip_text(
            _("Keep working in the background at a lower priority and notify when done"))
        self.background_button.connect("clicked", lambda _b: self.send_to_background())
        self.cancel_button = Gtk.Button(label=_("_Cancel"), use_underline=True)
        self.cancel_button.connect("clicked", lambda _b: self.cancel())
        for b in (self.pause_button, self.background_button, self.cancel_button):
            b.add_css_class("pill")
            buttons.append(b)
        page.append(buttons)
        self.stack.add_named(page, "progress")

        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key)
        self.add_controller(controller)
        self.connect("close-request", self._on_close_request)

        self._tick_source = GLib.timeout_add(120, self._tick)
        if show_delay > 0:
            GLib.timeout_add(show_delay, self._show_if_running)
        else:
            self.present()
        run_in_thread(lambda: self.job(self.callbacks), self._on_job_done)

    # -- visibility -----------------------------------------------------------
    def _show_if_running(self) -> bool:
        if self.running and not self.background and not self.get_visible():
            self.present()
        return False

    def ensure_visible(self) -> None:
        if not self.get_visible():
            self.background = False
            self.present()

    # -- prompts (called on the main loop by GtkCallbacks) -----------------------
    def prompt_password(self, archive_name: str, retry: bool, done) -> None:
        self.ensure_visible()
        prompts.ask_password(self, archive_name, retry, done)

    def prompt_overwrite(self, existing, incoming, done) -> None:
        self.ensure_visible()
        prompts.ask_overwrite(self, existing, incoming, done)

    # -- controls -------------------------------------------------------------
    def _on_pause(self, _button) -> None:
        if self.callbacks.paused:
            self.callbacks.resume()
            self.pause_button.set_label(_("_Pause"))
            self.bar.remove_css_class("paused")
        else:
            self.callbacks.pause()
            self.pause_button.set_label(_("_Resume"))
            self.bar.add_css_class("paused")
        self._tick()

    def send_to_background(self) -> None:
        self.background = True
        self.callbacks.lower_priority()
        self.set_visible(False)

    def cancel(self) -> None:
        if not self.running:
            self.close()
            return
        self.callbacks.cancel()
        self.cancel_button.set_sensitive(False)
        self.pause_button.set_sensitive(False)
        self.current_label.set_label(_("Cancelling…"))

    def _on_key(self, _controller, keyval, _keycode, _state) -> bool:
        from gi.repository import Gdk

        if keyval == Gdk.KEY_Escape and self.running:
            self._confirm_stop()
            return True
        return False

    def _on_close_request(self, _window) -> bool:
        if self.running:
            self._confirm_stop()
            return True
        self._stop_ticking()
        return False

    def _stop_ticking(self) -> None:
        if self._tick_source:
            GLib.source_remove(self._tick_source)
            self._tick_source = 0

    def _confirm_stop(self) -> None:
        dialog = prompts.alert(self, _("Stop {operation}?").format(operation=self.verb.lower()),
                               _("You can also let it finish in the background; you will be notified when it "
                                 "is done."))
        dialog.add_response("continue", _("_Continue"))
        dialog.add_response("background", _("Run in _Background"))
        dialog.add_response("stop", _("_Stop"))
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("continue")
        dialog.set_close_response("continue")

        def on_response(_d, response: str) -> None:
            if response == "stop":
                self.cancel()
            elif response == "background":
                self.send_to_background()

        dialog.connect("response", on_response)
        prompts.present(dialog, self)

    # -- progress -------------------------------------------------------------
    def _tick(self) -> bool:
        if not self.running:
            self._tick_source = 0
            return False
        cb = self.callbacks
        state = cb.snapshot()
        fraction = state.get("fraction")
        active = cb.active_seconds()
        stage = state.get("stage")
        current = state.get("current")
        if cb.paused:
            self.current_label.set_label(_("Paused"))
        elif cb.cancelled:
            self.current_label.set_label(_("Cancelling…"))
        elif current:
            self.current_label.set_label(current)
        elif stage:
            self.current_label.set_label(stage)
        if fraction is None:
            if not cb.paused:
                self.bar.pulse()
            self.percent_label.set_label("")
        else:
            self.bar.set_fraction(fraction)
            self.percent_label.set_label(f"{fraction * 100:.0f}%")
        percent = f"{fraction * 100:.0f}% · " if fraction is not None else ""
        self.set_title(percent + self.base_title)  # shown in the overview, like 7-Zip's taskbar title

        self.stat_elapsed.set(format_duration(active))
        done = state.get("done_bytes")
        total = state.get("total_bytes")
        now = time.monotonic()
        if done is not None:
            self._speed_samples.append((now, done))
            self._speed_samples = [s for s in self._speed_samples if now - s[0] <= 4.0]
        speed = None
        if len(self._speed_samples) >= 2 and not cb.paused:
            (t0, b0), (t1, b1) = self._speed_samples[0], self._speed_samples[-1]
            if t1 - t0 > 0.5:
                speed = max((b1 - b0) / (t1 - t0), 0)
        if speed is None and done and active > 1:
            speed = done / active
        self.stat_speed.set(_("{size}/s").format(size=format_size(int(speed))) if speed else "—")
        remaining = None
        if fraction is not None and 0.005 < fraction < 1 and active > 1.5:
            if speed and total:
                remaining = (total - (done or 0)) / speed if speed > 0 else None
            else:
                remaining = active * (1 - fraction) / fraction
        self.stat_remaining.set(format_duration(remaining))
        if total:
            self.stat_processed.set(_("{done} of {total}").format(done=format_size(done or 0),
                                                                  total=format_size(total)))
        elif done:
            self.stat_processed.set(format_size(done))
        files, total_files = state.get("files"), state.get("total_files")
        if files is not None and total_files:
            self.stat_files.set(_("{done} of {total}").format(done=files, total=total_files))
        elif files is not None:
            self.stat_files.set(str(files))
        packed = state.get("packed")
        if self.compressing:
            # 7-Zip's multi-threaded encoder only writes whole blocks, so the
            # ratio appears once real output exists (ZIP shows it right away).
            if packed and done and packed >= 256 * 1024 and done >= 4 * 1024 * 1024:
                self.stat_ratio.set(format_ratio(packed, done))
                self.stat_ratio.set_visible(True)
        elif packed:
            self.stat_ratio.set(format_size(packed))
            self.stat_ratio.set_visible(True)
        return True

    # -- completion -------------------------------------------------------------
    def _on_job_done(self, result, error) -> None:
        self.running = False
        elapsed = time.monotonic() - self._started
        self.callbacks.resume()
        if isinstance(error, Cancelled):
            self._stop_ticking()
            self.destroy()
            return
        app = self.get_application()
        try:
            self.finished_cb(self, result, error)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            self.show_status(icon_name="dialog-error-symbolic", title=_("Something Went Wrong"),
                             description=str(exc), style="error")
        notify = self.background or (elapsed > 8 and self.get_visible() and not self.is_active())
        if notify and app is not None and hasattr(app, "notify_finished"):
            app.notify_finished(self, result, error)
        if self.background and self.result_shown:
            self.background = False

    def done_quietly(self) -> None:
        """No result page needed: close the window."""
        self._stop_ticking()
        self.destroy()

    @staticmethod
    def _on_status_button(button: Gtk.Button, callback) -> None:
        feedback = callback()
        if isinstance(feedback, str):  # e.g. "Copied"
            button.set_label(feedback)

    def show_status(self, *, icon_name: str, title: str, description: str = "", style: str | None = None,
                    rows: list[tuple[str, str]] | None = None, details: str | None = None,
                    buttons: list[tuple[str, object, str | None]] | None = None, close_label: str | None = None,
                    list_rows: list[Gtk.Widget] | None = None, header: str | None = None,
                    monospace_rows: set[str] | None = None) -> None:
        """Replace the progress page with a result page."""
        self.result_shown = True
        self._stop_ticking()
        self.set_title(self.base_title)
        self.window_title.set_title(header or self.base_title)
        self.window_title.set_subtitle("")
        # The window stays non-resizable so GTK sizes it to the new content.

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=6, margin_bottom=24,
                      margin_start=24, margin_end=24)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(64)
        icon.add_css_class("status-icon")
        if style:
            icon.add_css_class(style)
        box.append(icon)
        heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER)
        heading.add_css_class("title-2")
        box.append(heading)
        if description:
            body = Gtk.Label(label=description, wrap=True, justify=Gtk.Justification.CENTER, selectable=False)
            body.add_css_class("body")
            box.append(body)
        if rows or list_rows:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            listbox.add_css_class("boxed-list")
            for row_title, row_value in rows or []:
                row = Adw.ActionRow(title=row_title, subtitle=row_value, subtitle_selectable=True, use_markup=False)
                row.add_css_class("property")
                if monospace_rows and row_title in monospace_rows:
                    row.add_css_class("hash-row")
                listbox.append(row)
            for widget in list_rows or []:
                listbox.append(widget)
            box.append(listbox)
        if details:
            expander = Gtk.Expander(label=_("Details"))
            text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
                                left_margin=10, right_margin=10)
            text.get_buffer().set_text(details)
            text.add_css_class("details-view")
            scroller = Gtk.ScrolledWindow(min_content_height=120, max_content_height=260,
                                          propagate_natural_height=True)
            scroller.set_child(text)
            scroller.add_css_class("card")
            copy = Gtk.Button(label=_("Copy Details"), halign=Gtk.Align.START, margin_top=6)
            copy.add_css_class("flat")
            copy.connect("clicked", lambda b: (copy_to_clipboard(b, details), b.set_label(_("Copied"))))
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6)
            inner.append(scroller)
            inner.append(copy)
            expander.set_child(inner)
            box.append(expander)

        actions = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=6)
        for label, callback, button_style in buttons or []:
            button = Gtk.Button(label=label, use_underline=True)
            button.add_css_class("pill")
            if button_style:
                button.add_css_class(button_style)
            button.connect("clicked", self._on_status_button, callback)
            actions.append(button)
        close = Gtk.Button(label=close_label or _("_Close"), use_underline=True)
        close.add_css_class("pill")
        if not buttons:
            close.add_css_class("suggested-action")
        close.connect("clicked", lambda _b: self.close())
        actions.append(close)
        box.append(actions)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=640)
        scroller.set_child(box)
        name = f"result-{id(box)}"
        self.stack.add_named(scroller, name)
        self.stack.set_visible_child_name(name)
        self.set_default_widget(close if not buttons else None)
        self.ensure_visible()
        close.grab_focus()
