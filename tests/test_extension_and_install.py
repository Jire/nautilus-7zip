# SPDX-License-Identifier: MIT
"""The Nautilus extension module and the installer."""

import importlib.util
import os
import subprocess

import pytest

from conftest import ROOT


def _load_extension():
    try:
        import gi

        for version in ("4.1", "4.0"):
            try:
                gi.require_version("Nautilus", version)
                break
            except ValueError:
                continue
        from gi.repository import Nautilus  # noqa: F401
    except (ImportError, ValueError):
        pytest.skip("nautilus-python's typelib is not installed")
    path = os.path.join(ROOT, "extension", "nautilus-7zip.py")
    spec = importlib.util.spec_from_file_location("nautilus_7zip_extension", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeFileInfo:
    def __init__(self, path, mime, is_dir=False):
        self.path, self.mime, self.dir = path, mime, is_dir

    def get_activation_uri(self):
        return "file://" + self.path

    def get_uri(self):
        return "file://" + self.path

    def get_location(self):
        from gi.repository import Gio

        return Gio.File.new_for_path(self.path)

    def get_mime_type(self):
        return self.mime

    def is_directory(self):
        return self.dir


def test_extension_builds_real_menu_items(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    module = _load_extension()
    provider = module.SevenZipMenuProvider()
    archive = tmp_path / "x.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    items = provider.get_file_items([FakeFileInfo(str(archive), "application/zip")])
    assert [i.props.label for i in items] == ["7-Zip"]
    # Nautilus 42 and older passed (window, files)
    assert provider.get_file_items(None, [FakeFileInfo(str(archive), "application/zip")])
    assert provider.get_background_items(None) == []


def test_extension_ignores_remote_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    module = _load_extension()

    class Remote(FakeFileInfo):
        def get_activation_uri(self):
            return "sftp://example.com/file.zip"

    provider = module.SevenZipMenuProvider()
    assert provider.get_file_items([Remote("/x.zip", "application/zip")]) == []


def test_install_and_uninstall_roundtrip(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_DATA_HOME="")
    env.pop("XDG_DATA_HOME")
    script = os.path.join(ROOT, "install.sh")
    subprocess.run(["bash", script, "--quiet"], check=True, env=env)
    data = home / ".local" / "share"
    assert (data / "nautilus-python" / "extensions" / "nautilus-7zip.py").is_file()
    assert (data / "nautilus-7zip" / "nautilus7zip" / "app.py").is_file()
    assert os.access(data / "nautilus-7zip" / "nautilus-7zip", os.X_OK)
    assert (data / "applications" / "io.github.jire.Nautilus7Zip.desktop").is_file()
    extension = (data / "nautilus-python" / "extensions" / "nautilus-7zip.py").read_text()
    assert "@PKGDATADIR@" not in extension and str(data / "nautilus-7zip") in extension
    version = subprocess.run([str(data / "nautilus-7zip" / "nautilus-7zip"), "--version"], env=env,
                             capture_output=True, text=True, timeout=60, check=False)
    assert "nautilus-7zip" in version.stdout + version.stderr or version.returncode == 0
    subprocess.run(["bash", str(data / "nautilus-7zip" / "uninstall.sh"), "--quiet"], check=True, env=env)
    assert not (data / "nautilus-7zip").exists()
    assert not (data / "nautilus-python" / "extensions" / "nautilus-7zip.py").exists()
