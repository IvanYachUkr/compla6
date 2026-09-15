"""Public, dependency-free canonical HCB1 bundle implementation."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import struct
from typing import Sequence


HCB_MAGIC = b"HCB1"
HCB_VERSION = 1
HCB_HEADER = struct.Struct(">4sBI")
HCB_PATH_LENGTH = struct.Struct(">I")
HCB_CONTENT_LENGTH = struct.Struct(">Q")
MAX_PATH_BYTES = 1_048_576


class Phase1DataError(ValueError):
    """Raised when a canonical object or Phase 1 source violates its contract."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_relative_path(value: str) -> bytes:
    if not value or "\\" in value or "\x00" in value:
        raise Phase1DataError(f"invalid canonical path: {value!r}")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or any(part in ("", ".", "..") for part in parsed.parts)
    ):
        raise Phase1DataError(f"non-canonical relative path: {value!r}")
    encoded = value.encode("utf-8")
    if not (1 <= len(encoded) <= MAX_PATH_BYTES):
        raise Phase1DataError(f"path length outside contract: {value!r}")
    return encoded


def encode_hcb(entries: Sequence[tuple[str, bytes]]) -> bytes:
    """Encode an ordered path/content sequence as one strict HCB1 bundle."""
    if not entries:
        raise Phase1DataError("an HCB1 bundle must contain at least one file")
    if len(entries) > 0xFFFFFFFF:
        raise Phase1DataError("too many files for HCB1")

    previous: str | None = None
    chunks: list[bytes] = [HCB_HEADER.pack(HCB_MAGIC, HCB_VERSION, len(entries))]
    for path, content in entries:
        if not isinstance(content, bytes):
            raise Phase1DataError("HCB1 content must be bytes")
        encoded_path = validate_relative_path(path)
        if previous is not None and path <= previous:
            raise Phase1DataError("HCB1 paths must be unique and strictly ordinal")
        previous = path
        chunks.extend(
            (
                HCB_PATH_LENGTH.pack(len(encoded_path)),
                encoded_path,
                HCB_CONTENT_LENGTH.pack(len(content)),
                content,
            )
        )
    return b"".join(chunks)


def decode_hcb(value: bytes) -> tuple[tuple[str, bytes], ...]:
    """Decode one HCB1 bundle and reject truncation, bad order, and trailing data."""
    if len(value) < HCB_HEADER.size:
        raise Phase1DataError("HCB1 value is shorter than its header")
    magic, version, count = HCB_HEADER.unpack_from(value, 0)
    if magic != HCB_MAGIC:
        raise Phase1DataError("bad HCB1 magic")
    if version != HCB_VERSION:
        raise Phase1DataError("unsupported HCB1 version")
    if count == 0:
        raise Phase1DataError("HCB1 file count must be positive")

    offset = HCB_HEADER.size
    previous: str | None = None
    result: list[tuple[str, bytes]] = []
    for _index in range(count):
        if offset + HCB_PATH_LENGTH.size > len(value):
            raise Phase1DataError("truncated HCB1 path length")
        (path_length,) = HCB_PATH_LENGTH.unpack_from(value, offset)
        offset += HCB_PATH_LENGTH.size
        if not (1 <= path_length <= MAX_PATH_BYTES):
            raise Phase1DataError("invalid HCB1 path length")
        if offset + path_length > len(value):
            raise Phase1DataError("truncated HCB1 path")
        raw_path = value[offset : offset + path_length]
        offset += path_length
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise Phase1DataError("HCB1 path is not valid UTF-8") from exc
        if validate_relative_path(path) != raw_path:
            raise Phase1DataError("HCB1 path is not canonically encoded")
        if previous is not None and path <= previous:
            raise Phase1DataError("HCB1 paths are duplicated or out of order")
        previous = path

        if offset + HCB_CONTENT_LENGTH.size > len(value):
            raise Phase1DataError("truncated HCB1 content length")
        (content_length,) = HCB_CONTENT_LENGTH.unpack_from(value, offset)
        offset += HCB_CONTENT_LENGTH.size
        if content_length > len(value) - offset:
            raise Phase1DataError("truncated HCB1 content")
        content = value[offset : offset + content_length]
        offset += content_length
        result.append((path, content))

    if offset != len(value):
        raise Phase1DataError("trailing bytes after HCB1 bundle")
    return tuple(result)
