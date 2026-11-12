# SPDX-License-Identifier: MIT
"""Translation support (gettext domain "nautilus-7zip")."""

from __future__ import annotations

import gettext as _gettext
import os

DOMAIN = "nautilus-7zip"

_translation: _gettext.NullTranslations = _gettext.NullTranslations()
_initialized = False


def locale_dirs() -> list[str]:
    dirs = []
    env = os.environ.get("NAUTILUS_7ZIP_LOCALEDIR")
    if env:
        dirs.append(env)
    package_dir = os.path.dirname(os.path.abspath(__file__))
    # <prefix>/share/nautilus-7zip/nautilus7zip → <prefix>/share/locale
    dirs.append(os.path.normpath(os.path.join(package_dir, "..", "..", "locale")))
    # Source checkout: <repo>/src/nautilus7zip → <repo>/build/locale
    dirs.append(os.path.normpath(os.path.join(package_dir, "..", "..", "build", "locale")))
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs.append(os.path.join(data_home, "locale"))
    dirs.extend(["/usr/local/share/locale", "/usr/share/locale"])
    return dirs


def setup() -> None:
    global _translation, _initialized
    if _initialized:
        return
    _initialized = True
    for directory in locale_dirs():
        try:
            _translation = _gettext.translation(DOMAIN, directory)
            return
        except OSError:
            continue
    _translation = _gettext.NullTranslations()


def _(message: str) -> str:
    if not _initialized:
        setup()
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    if not _initialized:
        setup()
    return _translation.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    if not _initialized:
        setup()
    return _translation.pgettext(context, message)


def N_(message: str) -> str:
    """Mark a string for translation without translating it yet."""
    return message
