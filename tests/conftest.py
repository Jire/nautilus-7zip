# SPDX-License-Identifier: MIT
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from nautilus7zip.engines.base import Answer, Callbacks  # noqa: E402

HAVE_7Z = any(shutil.which(n) for n in ("7z", "7zz", "7za"))


class ScriptedCallbacks(Callbacks):
    """Answers prompts from pre-recorded lists and records everything."""

    def __init__(self, passwords=(), answers=()):
        super().__init__()
        self.passwords = list(passwords)
        self.answers = list(answers)
        self.password_requests = []
        self.overwrite_requests = []
        self.stages = []
        self.fractions = []

    def stage(self, text):
        self.stages.append(text)

    def progress(self, fraction, **kwargs):
        self.fractions.append(fraction)

    def ask_password(self, archive_name, retry):
        self.password_requests.append((archive_name, retry))
        return self.passwords.pop(0) if self.passwords else None

    def ask_overwrite(self, existing, incoming):
        self.overwrite_requests.append((existing, incoming))
        return self.answers.pop(0) if self.answers else (Answer.SKIP, True)


@pytest.fixture
def callbacks():
    return ScriptedCallbacks


@pytest.fixture(params=["7zip", "builtin"])
def engine_mode(request, monkeypatch):
    """Run a test once with 7-Zip and once with only the built-in engines."""
    if request.param == "7zip":
        if not HAVE_7Z:
            pytest.skip("7-Zip is not installed")
        monkeypatch.delenv("NAUTILUS_7ZIP_DISABLE", raising=False)
    else:
        monkeypatch.setenv("NAUTILUS_7ZIP_DISABLE", "1")
    from nautilus7zip import tool

    tool._quick_cache = None
    return request.param


@pytest.fixture
def sample_tree(tmp_path):
    """photos/{a.txt, sub/b.txt, link -> a.txt, big.bin}"""
    root = tmp_path / "src" / "photos"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello\n")
    (root / "sub" / "b.txt").write_text("world\n")
    (root / "big.bin").write_bytes(os.urandom(300_000))
    (root / "ünïcødé ✓.txt").write_text("unicode\n")
    os.symlink("a.txt", root / "link")
    os.chmod(root / "a.txt", 0o640)
    return root
