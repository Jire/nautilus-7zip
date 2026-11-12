# SPDX-License-Identifier: MIT
"""The 7-Zip context menu as a plain data structure.

The Nautilus extension converts these nodes into ``Nautilus.MenuItem``
objects; keeping the logic here makes it testable without a file manager.
The layout mirrors 7-Zip's Explorer menu:

    7-Zip ▸ Open Archive
            Extract Files…
            Extract Here
            Extract to “name/”
            Test Archive
            Add to Archive…
            Compress and Email…
            Add to “name.7z”
            Compress to “name.7z” and Email
            Add to “name.zip”
            Compress to “name.zip” and Email
            CRC SHA ▸ CRC-32 … BLAKE2sp, All Checksums, SHA-256 → “name.sha256”
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import formats, hashalgos, tool
from .i18n import _

LABEL_LIMIT = 34


@dataclass
class Selected:
    path: str
    mime: str | None = None
    is_dir: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.path.rstrip(os.sep)) or self.path


@dataclass
class MenuNode:
    id: str
    label: str
    action: list[str] | None = None  # helper arguments
    children: list[MenuNode] = field(default_factory=list)
    sensitive: bool = True
    tip: str = ""


def _q(name: str) -> str:
    return formats.ellipsize_middle(name, LABEL_LIMIT)


def available_hash_algorithms(sevenzip_names: set[str]) -> list[hashalgos.HashAlgorithm]:
    """Algorithms we can compute with the installed 7-Zip builds (or Python)."""
    if sevenzip_names & {"7z", "7zz"}:
        return list(hashalgos.ALGORITHMS)
    native: set[str] = set()
    if "7za" in sevenzip_names:
        native = {"crc32", "crc64", "xxh64", "sha1", "sha256"}
    elif "7zr" in sevenzip_names:
        native = {"crc32", "crc64", "sha256"}
    return [a for a in hashalgos.ALGORITHMS if a.builtin or a.id in native]


def build(selection: list[Selected], settings, *, sevenzip_names: set[str] | None = None,
          is_a=None) -> list[MenuNode]:
    """Return the top-level menu nodes for a selection (may be empty)."""
    items = [s for s in selection if formats.is_local_path(s.path)]
    if not items or len(items) != len(selection):
        return []
    if sevenzip_names is None:
        sevenzip_names = {os.path.basename(p) for p in tool.candidates(settings.get("sevenzip_path") or None)}
    have_7zip = bool(sevenzip_names)
    can_7z_format = have_7zip  # every 7-Zip build (even 7zr) writes .7z

    paths = [s.path for s in items]
    archives: list[str] = []
    others = 0
    for s in items:
        fmt = None if s.is_dir else formats.detect(s.name, s.mime, is_a=is_a)
        if fmt is None or not settings.category_enabled(fmt.category):
            others += 1
            continue
        if fmt.needs_7zip and not have_7zip and fmt.category != formats.ARCHIVE:
            others += 1  # e.g. .iso/.exe: only offer them once 7-Zip can open them
            continue
        archives.append(s.path)
    archives = formats.dedupe_volumes(archives)

    nodes: list[MenuNode] = []
    enabled = settings.menu_item_enabled

    if archives:
        single = len(archives) == 1
        if single and others == 0 and enabled("open"):
            nodes.append(MenuNode("open", _("Open Archive"), ["open", archives[0]]))
        if enabled("extract"):
            nodes.append(MenuNode("extract", _("Extract Files…"), ["extract", "--dialog", "--", *archives]))
        if enabled("extract_here"):
            nodes.append(MenuNode("extract-here", _("Extract Here"), ["extract", "--here", "--", *archives]))
        if enabled("extract_to"):
            if single:
                stem = formats.archive_stem(os.path.basename(archives[0]))
                label = _("Extract to “{folder}/”").format(folder=_q(stem))
            else:
                label = _("Extract Each to Separate Folder")
            nodes.append(MenuNode("extract-to", label, ["extract", "--to-folder", "--", *archives]))
        if enabled("test"):
            label = _("Test Archive") if single else _("Test Archives")
            nodes.append(MenuNode("test", label, ["test", "--", *archives]))

    if len(items) == 1 and not items[0].is_dir and enabled("verify"):
        is_sum, _algo = formats.checksum_file_algorithm(items[0].name)
        if is_sum:
            nodes.append(MenuNode("verify", _("Verify Checksums"), ["verify", "--", items[0].path]))

    directories = {s.path for s in items if s.is_dir}
    base = formats.archive_name_for(paths, directories)
    folder = formats.common_parent(paths)
    # Never offer to add an archive to itself ("x.zip" → Add to "x.zip").
    selected = set(paths)
    can_add_7z = os.path.join(folder, base + ".7z") not in selected
    can_add_zip = os.path.join(folder, base + ".zip") not in selected
    if enabled("add"):
        nodes.append(MenuNode("add", _("Add to Archive…"), ["add", "--dialog", "--", *paths]))
    if enabled("add_email"):
        nodes.append(MenuNode("add-email", _("Compress and Email…"), ["add", "--dialog", "--email", "--", *paths]))
    if can_7z_format and can_add_7z and enabled("add_7z"):
        nodes.append(MenuNode("add-7z", _("Add to “{archive}”").format(archive=_q(base + ".7z")),
                              ["add", "--format", "7z", "--", *paths]))
    if can_7z_format and can_add_7z and enabled("add_7z_email"):
        nodes.append(MenuNode("add-7z-email",
                              _("Compress to “{archive}” and Email").format(archive=_q(base + ".7z")),
                              ["add", "--format", "7z", "--email", "--", *paths]))
    if can_add_zip and enabled("add_zip"):
        nodes.append(MenuNode("add-zip", _("Add to “{archive}”").format(archive=_q(base + ".zip")),
                              ["add", "--format", "zip", "--", *paths]))
    if can_add_zip and enabled("add_zip_email"):
        nodes.append(MenuNode("add-zip-email",
                              _("Compress to “{archive}” and Email").format(archive=_q(base + ".zip")),
                              ["add", "--format", "zip", "--email", "--", *paths]))

    if enabled("hash"):
        hash_menu = MenuNode("crc-sha", _("CRC SHA"))
        for algo in available_hash_algorithms(sevenzip_names):
            hash_menu.children.append(MenuNode(f"hash-{algo.id}", algo.label,
                                               ["hash", "--algorithm", algo.id, "--", *paths]))
        hash_menu.children.append(MenuNode("hash-all", _("All Checksums"), ["hash", "--algorithm", "all", "--",
                                                                            *paths]))
        if len(items) == 1 and not items[0].is_dir:
            target = items[0].name + ".sha256"
        else:
            target = base + ".sha256"
        hash_menu.children.append(MenuNode("hash-save", _("SHA-256 → “{file}”").format(file=_q(target)),
                                           ["hash", "--algorithm", "sha256", "--save", "--", *paths]))
        nodes.append(hash_menu)

    if not nodes:
        return []
    if settings.get("cascaded", True):
        return [MenuNode("menu", "7-Zip", children=nodes)]
    return nodes
