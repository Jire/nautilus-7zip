# SPDX-License-Identifier: MIT
import os

import pytest

from nautilus7zip import formats, menu
from nautilus7zip.settings import Settings


@pytest.mark.parametrize(
    "name, fmt, stem",
    [
        ("photos.7z", "7z", "photos"),
        ("photos.ZIP", "zip", "photos"),
        ("linux-6.12.tar.xz", "tar.xz", "linux-6.12"),
        ("pkg-1.0-1-x86_64.pkg.tar.zst", "tar.zst", "pkg-1.0-1-x86_64.pkg"),
        ("backup.tgz", "tar.gz", "backup"),
        ("notes.txt.gz", "gz", "notes.txt"),
        ("disk.iso", "iso", "disk"),
        ("report.docx", "ooxml", "report"),
        ("movie.part2.rar", "rar", "movie"),
        ("backup.7z.003", "7z", "backup"),
        ("data.001", "split", "data"),
        ("old.r00", "rar", "old"),
        ("split.z01", "zip", "split"),
    ],
)
def test_detect_and_stem(name, fmt, stem):
    detected = formats.detect(name)
    assert detected is not None and detected.id == fmt
    assert formats.archive_stem(name) == stem


@pytest.mark.parametrize("name", ["notes.txt", "photo.jpg", "data.002", "report.123", "README"])
def test_not_archives(name):
    assert formats.detect(name) is None


def test_mime_fallback_and_zip_subclass():
    assert formats.detect("download", "application/zip").id == "zip"
    fmt = formats.detect("drawing.kra", "application/x-krita", is_a=lambda m, p: p == "application/zip")
    assert fmt is not None and fmt.category == formats.DOCUMENT


def test_dedupe_volumes(tmp_path):
    for name in ("movie.part1.rar", "movie.part2.rar", "movie.part3.rar", "a.7z.001", "a.7z.002"):
        (tmp_path / name).write_bytes(b"")
    paths = [str(tmp_path / n) for n in ("movie.part3.rar", "a.7z.002", "movie.part2.rar", "single.zip")]
    assert formats.dedupe_volumes(paths) == [str(tmp_path / "movie.part1.rar"), str(tmp_path / "a.7z.001"),
                                              str(tmp_path / "single.zip")]


def test_unique_sibling(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"")
    (tmp_path / "photos.tar.gz").write_bytes(b"")
    (tmp_path / "folder").mkdir()
    assert os.path.basename(formats.unique_sibling(str(tmp_path / "report.pdf"))) == "report_1.pdf"
    assert os.path.basename(formats.unique_sibling(str(tmp_path / "photos.tar.gz"))) == "photos_1.tar.gz"
    assert os.path.basename(formats.unique_sibling(str(tmp_path / "folder"), is_dir=True)) == "folder_1"


def test_archive_name_for():
    assert formats.archive_name_for(["/home/u/report.pdf"], set()) == "report"
    assert formats.archive_name_for(["/home/u/Photos"], {"/home/u/Photos"}) == "Photos"
    assert formats.archive_name_for(["/home/u/a.txt", "/home/u/b.txt"], set()) == "u"
    assert formats.archive_name_for(["/home/u/app-1.0.tar.gz"], set()) == "app-1.0"


def test_checksum_files():
    assert formats.checksum_file_algorithm("SHA256SUMS") == (True, "sha256")
    assert formats.checksum_file_algorithm("ubuntu.iso.sha256") == (True, "sha256")
    assert formats.checksum_file_algorithm("files.md5") == (True, "md5")
    assert formats.checksum_file_algorithm("notes.txt")[0] is False


# ---------------------------------------------------------------------------
# Menu model
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return Settings(str(tmp_path / "settings.json"))


def labels(nodes):
    out = []
    for node in nodes:
        out.append(node.label)
        if node.children:
            out.append([*labels(node.children)])
    return out


def test_archive_menu_matches_7zip(settings):
    nodes = menu.build([menu.Selected("/d/photos.7z", "application/x-7z-compressed")], settings,
                       sevenzip_names={"7z"})
    assert len(nodes) == 1 and nodes[0].label == "7-Zip"
    items = [n.label for n in nodes[0].children]
    assert items == [
        "Open Archive", "Extract Files…", "Extract Here", "Extract to “photos/”", "Test Archive",
        "Add to Archive…", "Compress and Email…", "Add to “photos.zip”", "Compress to “photos.zip” and Email",
        "CRC SHA",
    ]  # "Add to “photos.7z”" is hidden: it would add the archive to itself
    crc = nodes[0].children[-1]
    assert [c.label for c in crc.children][:3] == ["CRC-32", "CRC-64", "XXH64"]
    assert crc.children[-1].label == "SHA-256 → “photos.7z.sha256”"


def test_plain_file_menu(settings):
    nodes = menu.build([menu.Selected("/d/notes.txt", "text/plain")], settings, sevenzip_names={"7z"})
    items = [n.label for n in nodes[0].children]
    assert items[0] == "Add to Archive…"
    assert "Add to “notes.7z”" in items and "Add to “notes.zip”" in items
    assert "Extract Here" not in items


def test_without_7zip(settings):
    nodes = menu.build([menu.Selected("/d/notes.txt", "text/plain")], settings, sevenzip_names=set())
    items = [n.label for n in nodes[0].children]
    assert "Add to “notes.zip”" in items and not any(".7z" in i for i in items)
    crc = next(n for n in nodes[0].children if n.label == "CRC SHA")
    assert "BLAKE2sp" not in [c.label for c in crc.children]
    # .7z archives still offer extraction (the helper then explains how to get 7-Zip) …
    nodes = menu.build([menu.Selected("/d/a.7z")], settings, sevenzip_names=set())
    assert "Extract Here" in [n.label for n in nodes[0].children]
    # … but disk images and installers only appear once 7-Zip can open them.
    nodes = menu.build([menu.Selected("/d/disk.iso")], settings, sevenzip_names=set())
    assert "Extract Here" not in [n.label for n in nodes[0].children]


def test_multiple_archives(settings):
    sel = [menu.Selected("/d/a.zip"), menu.Selected("/d/b.tar.gz")]
    items = [n.label for n in menu.build(sel, settings, sevenzip_names={"7z"})[0].children]
    assert "Open Archive" not in items
    assert "Extract Each to Separate Folder" in items
    assert "Test Archives" in items
    assert "Add to “d.7z”" in items


def test_flat_menu_and_hidden_items(settings):
    settings.set("cascaded", False)
    settings.set("menu_items.add_email", False)
    nodes = menu.build([menu.Selected("/d/a.zip")], settings, sevenzip_names={"7z"})
    assert nodes[0].label == "Open Archive"
    assert "Compress and Email…" not in [n.label for n in nodes]


def test_categories_can_be_disabled(settings):
    settings.set("categories.document", False)
    nodes = menu.build([menu.Selected("/d/report.docx")], settings, sevenzip_names={"7z"})
    assert "Extract Here" not in [n.label for n in nodes[0].children]


def test_non_local_selection_gets_no_menu(settings):
    assert menu.build([menu.Selected("relative/path.zip")], settings, sevenzip_names={"7z"}) == []


def test_checksum_file_gets_verify(settings):
    nodes = menu.build([menu.Selected("/d/SHA256SUMS")], settings, sevenzip_names={"7z"})
    assert "Verify Checksums" in [n.label for n in nodes[0].children]


def test_actions_are_helper_commands(settings):
    nodes = menu.build([menu.Selected("/d/x.zip")], settings, sevenzip_names={"7z"})
    by_id = {n.id: n for n in nodes[0].children}
    assert by_id["extract-here"].action == ["extract", "--here", "--", "/d/x.zip"]
    assert "add-zip" not in by_id  # would add x.zip to itself
    assert by_id["add-7z"].action == ["add", "--format", "7z", "--", "/d/x.zip"]
    assert by_id["test"].action == ["test", "--", "/d/x.zip"]


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    s = Settings(str(path))
    assert s.get("cascaded") is True
    s.set("menu_items.test", False)
    again = Settings(str(path))
    assert again.get("menu_items.test") is False
    path.write_text("{ not json")
    assert Settings(str(path)).get("cascaded") is True  # corrupt files fall back to defaults
