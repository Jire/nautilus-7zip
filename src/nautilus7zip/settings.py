# SPDX-License-Identifier: MIT
"""User preferences, stored as JSON in ``$XDG_CONFIG_HOME/nautilus-7zip``.

Pure Python: the Nautilus extension reads the same file (it re-reads it only
when the file's modification time changes, so a context menu never waits on
disk I/O beyond a single ``stat``).
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading

MENU_ITEMS = (
    "open",
    "extract",
    "extract_here",
    "extract_to",
    "test",
    "add",
    "add_email",
    "add_7z",
    "add_7z_email",
    "add_zip",
    "add_zip_email",
    "hash",
    "verify",
)

DEFAULTS: dict = {
    "version": 1,
    # Context menu
    "cascaded": True,
    "menu_items": {item: True for item in MENU_ITEMS},
    "categories": {"disk": True, "package": True, "document": True},
    # Extraction
    "eliminate_root_duplication": True,
    "smart_extract_here": False,
    "open_with": "builtin",  # builtin | 7zfm | default
    # Behaviour
    "notify_when_done": True,
    "sevenzip_path": "",  # empty = auto-detect
    # Remembered dialog values
    "extract": {
        "path_mode": "full",
        "overwrite": "ask",
        "recent_destinations": [],
    },
    "add": {
        "format": "7z",
        "level": 5,
        "store_symlinks": True,
        "update_mode": "add",
        "path_mode": "relative",
        "encrypt_names": False,
        "zip_encryption": "AES256",
        "threads": 0,
    },
}


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "nautilus-7zip")


def config_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "nautilus-7zip")


def _compatible(default, value) -> bool:
    """Accept a stored value only if it has the type the default expects."""
    if default is None or type(value) is type(default):
        return True
    numbers = (int, float)
    return isinstance(value, numbers) and isinstance(default, numbers) and not isinstance(value, bool)


def _merge(defaults: dict, loaded: dict) -> dict:
    result = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        elif key not in result or _compatible(result[key], value):
            result[key] = value
    return result


class Settings:
    """Lazily (re)loaded settings with atomic saves."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or config_path()
        self._mtime: float | None = None
        self._data: dict = copy.deepcopy(DEFAULTS)
        self._lock = threading.Lock()
        self._listeners: list = []

    @property
    def path(self) -> str:
        return self._path

    def _refresh(self) -> None:
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            if self._mtime is not None:
                self._data = copy.deepcopy(DEFAULTS)
                self._mtime = None
            return
        if mtime == self._mtime:
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = _merge(DEFAULTS, loaded)
        except (OSError, ValueError):
            self._data = copy.deepcopy(DEFAULTS)
        self._mtime = mtime

    def get(self, key: str, default=None):
        with self._lock:
            self._refresh()
            node = self._data
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._refresh()
            node = self._data
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            if node.get(parts[-1]) == value:
                return
            node[parts[-1]] = value
            self._save_locked()
        for listener in list(self._listeners):
            listener(key)

    def menu_item_enabled(self, item: str) -> bool:
        return bool(self.get(f"menu_items.{item}", True))

    def category_enabled(self, category: str) -> bool:
        if category == "archive":
            return True
        return bool(self.get(f"categories.{category}", True))

    def connect(self, callback) -> None:
        self._listeners.append(callback)

    def reset(self) -> None:
        with self._lock:
            self._data = copy.deepcopy(DEFAULTS)
            self._save_locked()
        for listener in list(self._listeners):
            listener("*")

    def _save_locked(self) -> None:
        directory = os.path.dirname(self._path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            self._mtime = os.stat(self._path).st_mtime
        except OSError:
            self._mtime = None
