# SPDX-License-Identifier: MIT
"""Parsing of 7-Zip's console output (recorded from 7-Zip 26.06)."""

from nautilus7zip import hashalgos, tool
from nautilus7zip.engines.sevenzip import _Terminal, parse_hash_output, parse_listing

LISTING = """
7-Zip 26.06 (x64) : Copyright (c) 1999-2026 Igor Pavlov : 2026-11-06

Listing archive: t.7z

--
Path = t.7z
Type = 7z
Physical Size = 3000414
Headers Size = 215
Method = LZMA2:3m
Solid = +
Blocks = 1

----------
Path = src
Size = 0
Packed Size = 0
Modified = 2026-11-12 14:36:40.3857592
Attributes = D drwxr-xr-x
CRC =
Encrypted = -
Method =
Block =

Path = src/a.txt
Size = 6
Packed Size = 3000199
Modified = 2026-11-12 14:36:40.3857592
Attributes = A -rw-r--r--
CRC = 363A3020
Encrypted = +
Method = LZMA2:3m 7zAES
Block = 0

Path = src/link
Size = 5
Packed Size =
Modified = 2026-11-12 14:36:40.3857592
Attributes = A lrwxrwxrwx
Symbolic Link = a.txt
CRC = 1234ABCD
Encrypted = -
Method = LZMA2:3m
Block = 0
"""

HASH = """CRC32    SHA256                                                                    Size  Name
-------- ---------------------------------------------------------------- -------------  ------------
363A3020 5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03             6  src/a.txt
                                                                                         src/sub/
DD3861A8 e258d248fda94c63753607f7c4494ee0fcbe92f1a76bfdac795c9d84101eb317             6  src/sub/b.txt
-------- ---------------------------------------------------------------- -------------  ------------
137291C8-00000001 3aea87fe1f7f2c6ce245f7a8d54428b318738f630b1b327dfce23f6d5714721b-00000000            12

Folders: 1
Files: 2
Size: 12

CRC32  for data:              137291C8-00000001
CRC32  for data and names:    51CFDBC1-00000001

SHA256 for data:              3aea87fe1f7f2c6ce245f7a8d54428b318738f630b1b327dfce23f6d5714721b-00000000
SHA256 for data and names:    cbec71b9f0684a69e3cb2c6a1343db97668fd4bfed226d09d3fd299387223339-00000001
"""

INFO = """
7-Zip 26.06 (x64) : Copyright (c) 1999-2026 Igor Pavlov : 2026-11-06

Libs:
 0 : 26.06 : /nonexistent/7z.so

Formats:
 0 C...F..........c.a.m+.. w...0  7z       7z            7 z BC AF ' 1C
 0  ......................  Ar       ar a deb udeb lib ! < a r c h > 0A
 0 CK.....................  bzip2    bz2 bzip2 tbz2 (.tar) tbz (.tar) B Z h
 0  ...F..................  Rar5     rar r00       R a r ! 1A 07 01 00
 0 C...FMG........c.a.m+.. wud.0  zip      zip z01 zipx jar xpi odt ods docx xlsx epub ipa apk appx P K 03 04

Codecs:
 0   D     40305 Rar5
 0  ED     30101 LZMA

Hashers:
      4        1 CRC32
 0   32        A SHA256
"""


def test_parse_listing():
    info = parse_listing(LISTING, "t.7z")
    assert info.type == "7z" and info.solid is True and info.blocks == 1
    assert [e.path for e in info.entries] == ["src", "src/a.txt", "src/link"]
    folder, file, link = info.entries
    assert folder.is_dir and not file.is_dir
    assert file.encrypted and file.crc == "363A3020" and file.size == 6
    assert link.link_target == "a.txt" and link.mode == 0o777
    assert file.mode == 0o644
    assert info.encrypted and info.total_size == 11 and info.folders == 1


def test_parse_hash_output():
    algorithms = [hashalgos.BY_ID["crc32"], hashalgos.BY_ID["sha256"]]
    table = parse_hash_output(HASH.splitlines(), algorithms)
    files = [r for r in table.rows if not r.is_dir]
    assert [r.name for r in files] == ["src/a.txt", "src/sub/b.txt"]
    assert files[0].digests == {"crc32": "363A3020",
                                "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"}
    assert table.data_sums["crc32"] == "137291C8-00000001"
    assert table.names_sums["sha256"].startswith("cbec71b9")
    assert table.files == 2 and table.total_size == 12


def test_parse_info():
    info = tool._parse_info("/usr/bin/7z", INFO)
    assert info.version == (26, 6)
    assert {"7z", "zip", "bzip2"} <= info.write_formats
    assert "ar" in info.read_formats and "ar" not in info.write_formats
    assert info.has_rar
    assert info.hashers == {"CRC32", "SHA256"}


def test_terminal_handles_backspace_progress():
    lines, progress = [], []
    term = _Terminal(lines.append, lambda *a: progress.append(a))
    term.feed("Scanning\n  0%\b\b\b\b    \b\b\b\b 42% 7 - dir/file.txt")
    term.feed("\b" * 22 + " " * 22 + "\b" * 22 + "Everything is Ok\nEnter password:")
    assert lines == ["Scanning", "Everything is Ok"]
    assert progress[-1] == (42, 7, "dir/file.txt")
    assert term.pending == "Enter password:"
