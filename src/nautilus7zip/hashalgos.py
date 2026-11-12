# SPDX-License-Identifier: MIT
"""Catalog of the checksum algorithms offered in the "CRC SHA" submenu.

Pure Python: shared by the Nautilus extension and the helper application.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass


@dataclass(frozen=True)
class HashAlgorithm:
    id: str  # stable identifier used on the command line
    label: str  # shown in menus and windows (same spelling as 7-Zip)
    sevenzip: str  # value for 7-Zip's -scrc switch / name in its output
    python: str | None  # hashlib name, "crc32", or None if only 7-Zip has it
    digest_size: int  # bytes
    file_extension: str  # extension used when saving a checksum file

    @property
    def builtin(self) -> bool:
        return self.python is not None


# Same order as 7-Zip's "CRC SHA" menu.
ALGORITHMS: tuple[HashAlgorithm, ...] = (
    HashAlgorithm("crc32", "CRC-32", "CRC32", "crc32", 4, "sfv"),
    HashAlgorithm("crc64", "CRC-64", "CRC64", None, 8, "crc64"),
    HashAlgorithm("xxh64", "XXH64", "XXH64", None, 8, "xxh64"),
    HashAlgorithm("md5", "MD5", "MD5", "md5", 16, "md5"),
    HashAlgorithm("sha1", "SHA-1", "SHA1", "sha1", 20, "sha1"),
    HashAlgorithm("sha256", "SHA-256", "SHA256", "sha256", 32, "sha256"),
    HashAlgorithm("sha384", "SHA-384", "SHA384", "sha384", 48, "sha384"),
    HashAlgorithm("sha512", "SHA-512", "SHA512", "sha512", 64, "sha512"),
    HashAlgorithm("sha3-256", "SHA3-256", "SHA3-256", "sha3_256", 32, "sha3-256"),
    HashAlgorithm("blake2sp", "BLAKE2sp", "BLAKE2sp", None, 32, "blake2sp"),
)

BY_ID = {a.id: a for a in ALGORITHMS}
BY_SEVENZIP = {a.sevenzip.upper(): a for a in ALGORITHMS}

# Algorithms understood when *verifying* checksum files (GNU/BSD formats).
VERIFY_ALGORITHMS = {
    "crc32": "crc32",
    "md5": "md5",
    "sha1": "sha1",
    "sha224": "sha224",
    "sha256": "sha256",
    "sha384": "sha384",
    "sha512": "sha512",
    "sha3-224": "sha3_224",
    "sha3-256": "sha3_256",
    "sha3-384": "sha3_384",
    "sha3-512": "sha3_512",
    "blake2b": "blake2b",
    "blake2s": "blake2s",
}

# Hex digest length → most likely algorithm, for files without a hint.
LENGTH_HINTS = {8: "crc32", 32: "md5", 40: "sha1", 56: "sha224", 64: "sha256", 96: "sha384", 128: "sha512"}


class Crc32:
    """hashlib-like wrapper around zlib.crc32."""

    name = "crc32"
    digest_size = 4

    def __init__(self) -> None:
        self._value = 0

    def update(self, data: bytes) -> None:
        self._value = zlib.crc32(data, self._value)

    def hexdigest(self) -> str:
        return f"{self._value & 0xFFFFFFFF:08X}"


def new_python_hasher(python_name: str):
    """Create an incremental hasher for a ``python`` algorithm name."""
    if python_name == "crc32":
        return Crc32()
    return hashlib.new(python_name)


def format_digest(algorithm_id: str, hexdigest: str) -> str:
    """7-Zip prints CRCs in upper case and the SHA family in lower case."""
    if algorithm_id in ("crc32", "crc64", "xxh64"):
        return hexdigest.upper()
    return hexdigest.lower()
