# SPDX-License-Identifier: MIT
"""End-to-end tests of create → test → extract, with and without 7-Zip."""

import io
import os
import tarfile
import zipfile

import pytest

from conftest import HAVE_7Z
from nautilus7zip import jobs
from nautilus7zip.engines.base import Answer, ArchiveError, ErrorKind, OverwriteMode


def _create(sample_tree, tmp_path, cb, fmt, **kwargs):
    ext = jobs.CREATE_FORMATS[fmt][1]
    archive = tmp_path / f"photos{ext}"
    request = jobs.CreateRequest(sources=[str(sample_tree)], archive=str(archive), format=fmt, **kwargs)
    report = jobs.create(request, cb)
    assert report.ok, report.errors
    assert archive.exists()
    return archive


def _check_tree(root):
    assert (root / "a.txt").read_text() == "hello\n"
    assert (root / "sub" / "b.txt").read_text() == "world\n"
    assert (root / "ünïcødé ✓.txt").read_text() == "unicode\n"
    assert len((root / "big.bin").read_bytes()) == 300_000


@pytest.mark.parametrize("fmt", ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_roundtrip_here(engine_mode, fmt, sample_tree, tmp_path, callbacks):
    cb = callbacks()
    archive = _create(sample_tree, tmp_path, cb, fmt)
    out = tmp_path / "out"
    out.mkdir()
    moved = out / archive.name
    archive.rename(moved)
    outcomes = jobs.extract(jobs.ExtractRequest([str(moved)], mode="here"), cb)
    assert outcomes[0].report.ok, outcomes[0].report.errors
    _check_tree(out / "photos")
    assert os.path.islink(out / "photos" / "link")
    assert os.readlink(out / "photos" / "link") == "a.txt"
    assert (os.stat(out / "photos" / "a.txt").st_mode & 0o777) == 0o640
    # no staging folders are left behind
    assert not [n for n in os.listdir(out) if n.startswith(jobs.STAGING_PREFIX)]


@pytest.mark.skipif(not HAVE_7Z, reason="7-Zip is not installed")
@pytest.mark.parametrize("fmt", ["7z", "wim"])
def test_roundtrip_7zip_only_formats(fmt, sample_tree, tmp_path, callbacks, monkeypatch):
    monkeypatch.delenv("NAUTILUS_7ZIP_DISABLE", raising=False)
    cb = callbacks()
    archive = _create(sample_tree, tmp_path, cb, fmt)
    outcomes = jobs.extract(jobs.ExtractRequest([str(archive)], mode="folder"), cb)
    assert outcomes[0].report.ok
    # "photos.7z" contains "photos/…" → root duplication eliminated
    _check_tree(tmp_path / "photos")
    assert not (tmp_path / "photos" / "photos").exists()


def test_extract_to_folder_without_elimination(engine_mode, sample_tree, tmp_path, callbacks):
    cb = callbacks()
    archive = _create(sample_tree, tmp_path, cb, "zip")
    shutil_target = tmp_path / "photos"
    import shutil

    shutil.rmtree(shutil_target, ignore_errors=True)
    req = jobs.ExtractRequest([str(archive)], mode="folder", eliminate_root=False)
    jobs.extract(req, cb)
    _check_tree(tmp_path / "photos" / "photos")


def test_smart_extract_here_creates_folder(engine_mode, tmp_path, callbacks):
    archive = tmp_path / "loose.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("one.txt", "1")
        zf.writestr("two.txt", "2")
    cb = callbacks()
    jobs.extract(jobs.ExtractRequest([str(archive)], mode="here", smart_here=True), cb)
    assert (tmp_path / "loose" / "one.txt").read_text() == "1"
    assert not (tmp_path / "one.txt").exists()


def test_conflicts_are_asked_and_honoured(engine_mode, tmp_path, callbacks):
    archive = tmp_path / "c.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("keep.txt", "new")
        zf.writestr("replace.txt", "new")
        zf.writestr("both.txt", "new")
    for name in ("keep.txt", "replace.txt", "both.txt"):
        (tmp_path / name).write_text("old")
    cb = callbacks(answers=[(Answer.KEEP_BOTH, False), (Answer.SKIP, False), (Answer.REPLACE, False)])
    jobs.extract(jobs.ExtractRequest([str(archive)], mode="here"), cb)
    assert len(cb.overwrite_requests) == 3
    assert (tmp_path / "both.txt").read_text() == "old"
    assert (tmp_path / "both_1.txt").read_text() == "new"
    assert (tmp_path / "keep.txt").read_text() == "old"
    assert (tmp_path / "replace.txt").read_text() == "new"


def test_overwrite_modes(engine_mode, tmp_path, callbacks):
    archive = tmp_path / "m.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("f.txt", "new")
    (tmp_path / "f.txt").write_text("old")
    jobs.extract(jobs.ExtractRequest([str(archive)], overwrite=OverwriteMode.RENAME_EXISTING), callbacks())
    assert (tmp_path / "f.txt").read_text() == "new"
    assert (tmp_path / "f_1.txt").read_text() == "old"
    jobs.extract(jobs.ExtractRequest([str(archive)], overwrite=OverwriteMode.SKIP), callbacks())
    assert (tmp_path / "f.txt").read_text() == "new"


def test_cancel_leaves_nothing_behind(engine_mode, tmp_path, callbacks):
    archive = tmp_path / "x.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("x/a.txt", "a")
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "a.txt").write_text("old")
    cb = callbacks(answers=[(Answer.CANCEL, False)])
    from nautilus7zip.engines.base import Cancelled

    with pytest.raises(Cancelled):
        jobs.extract(jobs.ExtractRequest([str(archive)], mode="here"), cb)
    assert (tmp_path / "x" / "a.txt").read_text() == "old"
    assert sorted(os.listdir(tmp_path)) == ["x", "x.zip"]


def test_zip_slip_is_blocked(engine_mode, tmp_path, callbacks):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escaped.txt", "pwned")
        zf.writestr("ok.txt", "fine")
    dest = tmp_path / "d"
    dest.mkdir()
    moved = dest / "evil.zip"
    archive.rename(moved)
    outcomes = jobs.extract(jobs.ExtractRequest([str(moved)], mode="here"), callbacks())
    assert (dest / "ok.txt").read_text() == "fine"
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert outcomes


def test_tar_symlink_escape_is_blocked(monkeypatch, tmp_path, callbacks):
    monkeypatch.setenv("NAUTILUS_7ZIP_DISABLE", "1")
    archive = tmp_path / "evil.tar.gz"
    outside = tmp_path / "outside"
    outside.mkdir()
    with tarfile.open(archive, "w:gz") as tf:
        link = tarfile.TarInfo("trap")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tf.addfile(link)
        data = b"pwned"
        member = tarfile.TarInfo("trap/owned.txt")
        member.size = len(data)
        tf.addfile(member, io.BytesIO(data))
    dest = tmp_path / "dest"
    dest.mkdir()
    moved = dest / archive.name
    archive.rename(moved)
    outcomes = jobs.extract(jobs.ExtractRequest([str(moved)], mode="here"), callbacks())
    assert not (outside / "owned.txt").exists()
    assert any("unsafe" in w.lower() for w in outcomes[0].report.warnings)


@pytest.mark.skipif(not HAVE_7Z, reason="7-Zip is not installed")
def test_encrypted_7z_password_flow(sample_tree, tmp_path, callbacks, monkeypatch):
    monkeypatch.delenv("NAUTILUS_7ZIP_DISABLE", raising=False)
    archive = _create(sample_tree, tmp_path, callbacks(), "7z", password="s3cret", encrypt_names=True)
    # wrong, then right
    cb = callbacks(passwords=["nope", "s3cret"])
    out = tmp_path / "o"
    out.mkdir()
    moved = out / archive.name
    archive.rename(moved)
    jobs.extract(jobs.ExtractRequest([str(moved)], mode="here"), cb)
    assert [retry for _name, retry in cb.password_requests] == [False, True]
    _check_tree(out / "photos")


@pytest.mark.skipif(not HAVE_7Z, reason="7-Zip is not installed")
def test_encrypted_zip_data_password_flow(sample_tree, tmp_path, callbacks, monkeypatch):
    monkeypatch.delenv("NAUTILUS_7ZIP_DISABLE", raising=False)
    archive = _create(sample_tree, tmp_path, callbacks(), "zip", password="pw", zip_encryption="ZipCrypto")
    for mode in ("7zip", "builtin"):
        if mode == "builtin":
            monkeypatch.setenv("NAUTILUS_7ZIP_DISABLE", "1")
        out = tmp_path / f"o-{mode}"
        out.mkdir()
        cb = callbacks(passwords=["bad", "pw"])
        jobs.extract(jobs.ExtractRequest([str(archive)], mode="custom", destination=str(out)), cb)
        assert [retry for _n, retry in cb.password_requests] == [False, True], mode
        _check_tree(out / "photos")


def test_aes_zip_without_7zip_explains(monkeypatch, sample_tree, tmp_path, callbacks):
    if not HAVE_7Z:
        pytest.skip("needs 7-Zip to create the AES archive")
    archive = _create(sample_tree, tmp_path, callbacks(), "zip", password="pw")
    monkeypatch.setenv("NAUTILUS_7ZIP_DISABLE", "1")
    with pytest.raises(ArchiveError) as info:
        jobs.extract(jobs.ExtractRequest([str(archive)], mode="folder"), callbacks(passwords=["pw"]))
    assert info.value.kind is ErrorKind.NEEDS_7ZIP


def test_test_reports_damage(engine_mode, sample_tree, tmp_path, callbacks):
    archive = _create(sample_tree, tmp_path, callbacks(), "zip", level=0)
    data = bytearray(archive.read_bytes())
    pos = data.find(b"world")
    assert pos > 0
    data[pos] ^= 0xFF
    archive.write_bytes(bytes(data))
    outcomes = jobs.test([str(archive)], callbacks())
    assert not outcomes[0].report.ok


def test_test_ok(engine_mode, sample_tree, tmp_path, callbacks):
    archive = _create(sample_tree, tmp_path, callbacks(), "tar.gz")
    outcomes = jobs.test([str(archive)], callbacks())
    assert outcomes[0].report.ok, outcomes[0].report.errors
    assert outcomes[0].report.files >= 4


def test_truncated_tarball(engine_mode, sample_tree, tmp_path, callbacks):
    archive = _create(sample_tree, tmp_path, callbacks(), "tar.gz")
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    outcomes = jobs.test([str(archive)], callbacks())
    assert outcomes[0].report.error_kind in (ErrorKind.TRUNCATED, ErrorKind.DAMAGED)


def test_not_an_archive(engine_mode, tmp_path, callbacks):
    bogus = tmp_path / "fake.zip"
    bogus.write_text("definitely not a zip")
    outcomes = jobs.test([str(bogus)], callbacks())
    assert outcomes[0].report.error_kind is ErrorKind.NOT_ARCHIVE


def test_needs_7zip_for_7z(monkeypatch, tmp_path, callbacks):
    monkeypatch.setenv("NAUTILUS_7ZIP_DISABLE", "1")
    archive = tmp_path / "x.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
    with pytest.raises(ArchiveError) as info:
        jobs.extract(jobs.ExtractRequest([str(archive)]), callbacks())
    assert info.value.kind is ErrorKind.NEEDS_7ZIP


def test_single_file_streams(engine_mode, tmp_path, callbacks):
    src = tmp_path / "notes.txt"
    src.write_text("compress me " * 1000)
    for fmt in ("gz", "bz2", "xz"):
        request = jobs.CreateRequest([str(src)], str(tmp_path / f"notes.txt.{fmt}"), format=fmt)
        jobs.create(request, callbacks())
        out = tmp_path / f"out-{fmt}"
        jobs.extract(jobs.ExtractRequest([str(tmp_path / f"notes.txt.{fmt}")], mode="custom",
                                         destination=str(out)), callbacks())
        assert (out / "notes.txt").read_text() == "compress me " * 1000


def test_zip_update_replaces_entries(engine_mode, tmp_path, callbacks):
    a = tmp_path / "a.txt"
    a.write_text("v1")
    archive = tmp_path / "u.zip"
    jobs.create(jobs.CreateRequest([str(a)], str(archive), format="zip"), callbacks())
    a.write_text("v2")
    b = tmp_path / "b.txt"
    b.write_text("b")
    jobs.create(jobs.CreateRequest([str(a), str(b)], str(archive), format="zip"), callbacks())
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert sorted(names) == ["a.txt", "b.txt"]
        assert zf.read("a.txt") == b"v2"


def test_checksums_and_verify(engine_mode, sample_tree, tmp_path, callbacks):
    import hashlib

    result = jobs.compute_checksums([str(sample_tree / "a.txt"), str(sample_tree / "sub")], ["sha256", "crc32"],
                                    callbacks())
    rows = {r.name: r for r in result.rows if not r.is_dir}
    assert rows["a.txt"].digests["sha256"] == hashlib.sha256(b"hello\n").hexdigest()
    assert rows["a.txt"].digests["crc32"] == "363A3020"
    target = sample_tree / "checks.sha256"
    jobs.write_checksum_file(result, "sha256", str(target))
    verified = jobs.verify_checksums(str(target), callbacks())
    assert {r.status for r in verified} == {"ok"}
    (sample_tree / "a.txt").write_text("tampered")
    verified = jobs.verify_checksums(str(target), callbacks())
    assert {r.name: r.status for r in verified}["a.txt"] == "failed"


def test_browser_selection_strip_prefix(engine_mode, tmp_path, callbacks):
    archive = tmp_path / "deep.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("top/inner/wanted.txt", "w")
        zf.writestr("top/inner/also/x.txt", "x")
        zf.writestr("top/other.txt", "o")
    out = tmp_path / "out"
    req = jobs.ExtractRequest([str(archive)], mode="custom", destination=str(out),
                              members=["top/inner/wanted.txt", "top/inner/also"], strip_prefix="top/inner")
    jobs.extract(req, callbacks())
    assert sorted(os.listdir(out)) == ["also", "wanted.txt"]
    assert (out / "also" / "x.txt").read_text() == "x"


def test_flatten(engine_mode, tmp_path, callbacks):
    archive = tmp_path / "flat.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a/same.txt", "1")
        zf.writestr("b/same.txt", "2")
    out = tmp_path / "out"
    jobs.extract(jobs.ExtractRequest([str(archive)], mode="custom", destination=str(out), flatten=True),
                 callbacks())
    assert sorted(os.listdir(out)) == ["same.txt", "same_1.txt"]


def test_stale_staging_folders_are_swept(engine_mode, tmp_path, callbacks):
    stale = tmp_path / (jobs.STAGING_PREFIX + "leftover")
    stale.mkdir()
    (stale / "half-written.bin").write_bytes(b"x")
    old = 1_000_000_000
    os.utime(stale, (old, old))
    fresh = tmp_path / (jobs.STAGING_PREFIX + "in-use")
    fresh.mkdir()
    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("s.txt", "s")
    jobs.extract(jobs.ExtractRequest([str(archive)], mode="here"), callbacks())
    assert not stale.exists()
    assert fresh.exists()  # recent ones may belong to a running extraction
