"""Strict packed transport for Phase 1 throughput measurements.

The stream is benchmark transport only. Candidate archive sizes continue to be
measured from independently decodable per-object archives produced by the
directory correctness interface.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import struct
from typing import Iterable, Sequence


ORIGINAL_MAGIC = b"HBI1"
ARCHIVE_MAGIC = b"HBA1"
STREAM_VERSION = 1
HEADER = struct.Struct(">4sBI")
ALIAS_LENGTH = struct.Struct(">I")
PAYLOAD_LENGTH = struct.Struct(">Q")
MAX_ALIAS_BYTES = 1_048_576
MAX_RECORDS = 1_000_000
ALIAS_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class Phase1StreamError(ValueError):
    """Raised when packed benchmark transport is non-canonical or malformed."""


@dataclass(frozen=True)
class StreamRecord:
    alias: str
    payload: bytes


def _validated_records(
    records: Iterable[StreamRecord | tuple[str, bytes]],
) -> tuple[StreamRecord, ...]:
    result: list[StreamRecord] = []
    previous: str | None = None
    for value in records:
        record = value if isinstance(value, StreamRecord) else StreamRecord(*value)
        if not isinstance(record.alias, str) or not ALIAS_RE.fullmatch(record.alias):
            raise Phase1StreamError(f"invalid stream alias: {record.alias!r}")
        alias_bytes = record.alias.encode("utf-8")
        if not 1 <= len(alias_bytes) <= MAX_ALIAS_BYTES:
            raise Phase1StreamError("stream alias length is outside the allowed range")
        if previous is not None and record.alias <= previous:
            raise Phase1StreamError("stream aliases must be unique and strictly increasing")
        if not isinstance(record.payload, bytes):
            raise Phase1StreamError("stream payload must be bytes")
        result.append(record)
        previous = record.alias
    if not result:
        raise Phase1StreamError("a packed stream must contain at least one record")
    if len(result) > MAX_RECORDS:
        raise Phase1StreamError("packed stream record count exceeds the limit")
    return tuple(result)


def pack_stream(
    magic: bytes, records: Sequence[StreamRecord | tuple[str, bytes]]
) -> bytes:
    if magic not in (ORIGINAL_MAGIC, ARCHIVE_MAGIC):
        raise Phase1StreamError("unknown packed stream magic")
    checked = _validated_records(records)
    output = bytearray(HEADER.pack(magic, STREAM_VERSION, len(checked)))
    for record in checked:
        alias = record.alias.encode("utf-8")
        output.extend(ALIAS_LENGTH.pack(len(alias)))
        output.extend(alias)
        output.extend(PAYLOAD_LENGTH.pack(len(record.payload)))
        output.extend(record.payload)
    return bytes(output)


def unpack_stream(value: bytes, expected_magic: bytes) -> tuple[StreamRecord, ...]:
    if expected_magic not in (ORIGINAL_MAGIC, ARCHIVE_MAGIC):
        raise Phase1StreamError("unknown expected stream magic")
    if len(value) < HEADER.size:
        raise Phase1StreamError("truncated packed stream header")
    magic, version, count = HEADER.unpack_from(value)
    if magic != expected_magic:
        raise Phase1StreamError("packed stream magic mismatch")
    if version != STREAM_VERSION:
        raise Phase1StreamError("unsupported packed stream version")
    if not 1 <= count <= MAX_RECORDS:
        raise Phase1StreamError("invalid packed stream record count")

    offset = HEADER.size
    records: list[StreamRecord] = []
    previous: str | None = None
    for _index in range(count):
        if len(value) - offset < ALIAS_LENGTH.size:
            raise Phase1StreamError("truncated packed stream alias length")
        (alias_length,) = ALIAS_LENGTH.unpack_from(value, offset)
        offset += ALIAS_LENGTH.size
        if not 1 <= alias_length <= MAX_ALIAS_BYTES or alias_length > len(value) - offset:
            raise Phase1StreamError("invalid packed stream alias length")
        alias_bytes = value[offset : offset + alias_length]
        offset += alias_length
        try:
            alias = alias_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Phase1StreamError("stream alias is not valid UTF-8") from exc
        if not ALIAS_RE.fullmatch(alias):
            raise Phase1StreamError(f"invalid stream alias: {alias!r}")
        if previous is not None and alias <= previous:
            raise Phase1StreamError("stream aliases are duplicated or out of order")
        if len(value) - offset < PAYLOAD_LENGTH.size:
            raise Phase1StreamError("truncated packed stream payload length")
        (payload_length,) = PAYLOAD_LENGTH.unpack_from(value, offset)
        offset += PAYLOAD_LENGTH.size
        if payload_length > len(value) - offset:
            raise Phase1StreamError("packed stream payload exceeds remaining bytes")
        payload = value[offset : offset + payload_length]
        offset += payload_length
        records.append(StreamRecord(alias=alias, payload=payload))
        previous = alias
    if offset != len(value):
        raise Phase1StreamError("trailing bytes after packed stream")
    return tuple(records)


def replace_payloads(
    input_records: Sequence[StreamRecord], payloads: dict[str, bytes]
) -> tuple[StreamRecord, ...]:
    expected = {record.alias for record in input_records}
    if set(payloads) != expected:
        raise Phase1StreamError("replacement payload aliases do not match the stream")
    return tuple(StreamRecord(record.alias, payloads[record.alias]) for record in input_records)
