"""RLB1: checked relational framing without normalizing source spelling.

CSV is a lexical transport, not a request to reserialize using a CSV writer.
Schema bytes, table order, row order, quotes, null markers and terminators are
all canonical input bytes and must survive a static codec unchanged.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import struct
from typing import Any

from ..util import Error, pairs

MAGIC = b"RLB1"
MAX_BUNDLE = 64 * 1024 * 1024
MAX_SCHEMA = 1024 * 1024
MAX_TABLES = 64
MAX_ROWS = 1_000_000
INTEGER = re.compile(rb"[+-]?[0-9]+\Z")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


@dataclasses.dataclass(frozen=True)
class Record:
    raw: bytes
    cells: tuple[bytes | None, ...]
    terminator: bytes
    values: tuple[Any, ...] = ()


@dataclasses.dataclass(frozen=True)
class Bundle:
    schema_bytes: bytes
    schema: dict
    tables: tuple[tuple[str, bytes], ...]
    rows: dict[str, tuple[Record, ...]]


def csv_records(data: bytes) -> tuple[Record, ...]:
    """Strict byte scanner; CR/LF inside quotes are not record boundaries."""
    if not isinstance(data, bytes) or len(data) > MAX_BUNDLE:
        raise Error("relational_table_limit")
    rows: list[Record] = []
    i, n = 0, len(data)
    while i < n:
        start = i
        cells: list[bytes | None] = []
        term = b""
        while True:
            if i < n and data[i] == 34:
                i += 1
                decoded = bytearray()
                while True:
                    if i >= n:
                        raise Error("relational_unclosed_quote")
                    c = data[i]
                    i += 1
                    if c == 34:
                        if i < n and data[i] == 34:
                            decoded.append(34)
                            i += 1
                        else:
                            break
                    else:
                        decoded.append(c)
                if i < n and data[i] not in (44, 10, 13):
                    raise Error("relational_after_quote")
                cells.append(bytes(decoded))
            else:
                p = i
                while i < n and data[i] not in (44, 10, 13):
                    if data[i] == 34:
                        raise Error("relational_bare_quote")
                    i += 1
                token = data[p:i]
                cells.append(None if token == b"\\N" else token)
            if i < n and data[i] == 44:
                i += 1
                continue
            if i < n:
                p = i
                i += 1
                if data[p] == 13 and i < n and data[i] == 10:
                    i += 1
                term = data[p:i]
            rows.append(Record(data[start:i], tuple(cells), term))
            if len(rows) > MAX_ROWS:
                raise Error("relational_row_limit")
            break
    return tuple(rows)


def _schema(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_SCHEMA:
        raise Error("relational_schema_limit")
    try:
        s = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(Error("nonfinite_json")))
    except (UnicodeError, ValueError) as exc:
        raise Error("relational_schema_json", str(exc)) from exc
    if (not isinstance(s, dict) or set(s) != {"version", "dialect", "tables"}
            or type(s["version"]) is not int or s["version"] != 1
            or s["dialect"] != "csv-lexical-v1"
            or not isinstance(s["tables"], list) or not 1 <= len(s["tables"]) <= MAX_TABLES):
        raise Error("relational_schema_contract")
    names: set[str] = set()
    for t in s["tables"]:
        if (not isinstance(t, dict) or set(t) - {"name", "columns", "primary_key", "foreign_keys"}
                or not isinstance(t.get("name"), str) or not t["name"] or "\0" in t["name"]
                or len(t["name"].encode("utf-8")) > 1024 or t["name"] in names
                or not isinstance(t.get("columns"), list) or not 1 <= len(t["columns"]) <= 128):
            raise Error("relational_table_schema")
        names.add(t["name"])
        columns: set[str] = set()
        for c in t["columns"]:
            if (not isinstance(c, dict) or set(c) - {"name", "type", "nullable", "derived"}
                    or not isinstance(c.get("name"), str) or not c["name"]
                    or c["name"] in columns or c.get("type") not in ("int64", "uint64", "text", "bytes")
                    or type(c.get("nullable")) is not bool):
                raise Error("relational_column_schema")
            columns.add(c["name"])
        pk = t.get("primary_key", [])
        if (not isinstance(pk, list) or len(set(pk)) != len(pk)
                or any(k not in columns for k in pk)):
            raise Error("relational_primary_key_schema")
        for c in t["columns"]:
            d = c.get("derived")
            if d is not None:
                if (not isinstance(d, dict) or d.get("source") not in columns
                        or d.get("kind") not in ("copy", "prefix_id")):
                    raise Error("relational_derived_schema")
                expected = {"kind", "source"} if d["kind"] == "copy" else {"kind", "source", "prefix", "width"}
                if set(d) != expected:
                    raise Error("relational_derived_schema")
                if d["kind"] == "prefix_id" and (not isinstance(d["prefix"], str)
                        or type(d["width"]) is not int or not 0 <= d["width"] <= 128):
                    raise Error("relational_derived_schema")
        if not isinstance(t.get("foreign_keys", []), list):
            raise Error("relational_foreign_key_schema")
    by_name = {t["name"]: t for t in s["tables"]}
    for t in s["tables"]:
        for fk in t.get("foreign_keys", []):
            if (not isinstance(fk, dict) or set(fk) != {"columns", "table", "references"}
                    or fk["table"] not in by_name or not isinstance(fk["columns"], list)
                    or not fk["columns"] or not isinstance(fk["references"], list)
                    or len(fk["columns"]) != len(fk["references"])
                    or fk["references"] != by_name[fk["table"]].get("primary_key")
                    or any(x not in [c["name"] for c in t["columns"]] for x in fk["columns"])):
                raise Error("relational_foreign_key_schema")
    return s


def typed_records(table: dict, data: bytes) -> tuple[Record, ...]:
    columns = table["columns"]
    positions = {c["name"]: i for i, c in enumerate(columns)}
    answer = []
    for row in csv_records(data):
        if len(row.cells) != len(columns):
            raise Error("relational_column_count", table["name"])
        values = []
        for c, cell in zip(columns, row.cells):
            if cell is None:
                if not c["nullable"]:
                    raise Error("relational_null_constraint", c["name"])
                values.append(None)
            elif c["type"] in ("int64", "uint64"):
                # Bound lexical work too. Leading zero spellings remain legal.
                if len(cell) > 4096 or not INTEGER.fullmatch(cell):
                    raise Error("relational_integer_spelling")
                value = int(cell)
                low, high = (0, (1 << 64) - 1) if c["type"] == "uint64" else (-(1 << 63), (1 << 63) - 1)
                if not low <= value <= high:
                    raise Error("relational_integer_range")
                values.append(value)
            elif c["type"] == "text":
                try:
                    values.append(cell.decode("utf-8"))
                except UnicodeError as exc:
                    raise Error("relational_text_encoding") from exc
            else:
                values.append(cell)
        for i, c in enumerate(columns):
            d = c.get("derived")
            if d:
                source = values[positions[d["source"]]]
                if d["kind"] == "copy":
                    wanted = source
                else:
                    if type(source) is not int or source < 0:
                        raise Error("relational_derived_source")
                    wanted = d["prefix"] + str(source).zfill(d["width"])
                if values[i] != wanted:
                    raise Error("relational_derived_mismatch", c["name"])
        answer.append(dataclasses.replace(row, values=tuple(values)))
    return tuple(answer)


def validate_tables(schema: dict, tables: tuple[tuple[str, bytes], ...]) -> dict[str, tuple[Record, ...]]:
    if [n for n, _ in tables] != [t["name"] for t in schema["tables"]]:
        raise Error("relational_table_order_or_names")
    rows = {t["name"]: typed_records(t, data) for t, (_, data) in zip(schema["tables"], tables)}
    indexes = {}
    for t in schema["tables"]:
        positions = {c["name"]: i for i, c in enumerate(t["columns"])}
        pk = t.get("primary_key", [])
        if pk:
            keys = [tuple(r.values[positions[k]] for k in pk) for r in rows[t["name"]]]
            if any(None in k for k in keys) or len(set(keys)) != len(keys):
                raise Error("relational_primary_key_violation", t["name"])
            indexes[t["name"]] = set(keys)
    for t in schema["tables"]:
        positions = {c["name"]: i for i, c in enumerate(t["columns"])}
        for fk in t.get("foreign_keys", []):
            for r in rows[t["name"]]:
                key = tuple(r.values[positions[k]] for k in fk["columns"])
                if None not in key and key not in indexes[fk["table"]]:
                    raise Error("relational_foreign_key_violation", t["name"])
    return rows


def unpack(data: bytes) -> Bundle:
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_BUNDLE:
        raise Error("relational_frame_size")
    if data[:4] != MAGIC or hashlib.sha256(data[:-32]).digest() != data[-32:]:
        raise Error("relational_frame_digest")
    end, offset = len(data) - 32, 4

    def take(n: int) -> bytes:
        nonlocal offset
        if n < 0 or offset + n > end:
            raise Error("relational_frame_truncated")
        b = data[offset:offset + n]
        offset += n
        return b

    schema_size = struct.unpack("<I", take(4))[0]
    if schema_size > MAX_SCHEMA:
        raise Error("relational_schema_limit")
    schema_bytes = take(schema_size)
    schema = _schema(schema_bytes)
    count = struct.unpack("<I", take(4))[0]
    if not 1 <= count <= MAX_TABLES:
        raise Error("relational_table_count")
    tables = []
    for _ in range(count):
        length = struct.unpack("<H", take(2))[0]
        if not 1 <= length <= 1024:
            raise Error("relational_name_limit")
        try:
            name = take(length).decode("utf-8")
        except UnicodeError as exc:
            raise Error("relational_name_encoding") from exc
        size = struct.unpack("<Q", take(8))[0]
        tables.append((name, take(size)))
    if offset != end:
        raise Error("relational_trailing_bytes")
    ordered = tuple(tables)
    rows = validate_tables(schema, ordered)
    return Bundle(schema_bytes, schema, ordered, rows)


def pack(schema_bytes: bytes, tables) -> bytes:
    parts = [MAGIC, struct.pack("<I", len(schema_bytes)), schema_bytes]
    tables = tuple(tables)
    if len(tables) > MAX_TABLES:
        raise Error("relational_table_count")
    parts.append(struct.pack("<I", len(tables)))
    for name, data in tables:
        name_bytes = name.encode("utf-8")
        if not 1 <= len(name_bytes) <= 1024 or not isinstance(data, bytes):
            raise Error("relational_table_frame")
        parts.extend((struct.pack("<H", len(name_bytes)), name_bytes,
                      struct.pack("<Q", len(data)), data))
    body = b"".join(parts)
    framed = body + hashlib.sha256(body).digest()
    unpack(framed)
    return framed


def append_record(existing: bytes, record: bytes) -> bytes:
    """Defined insert framing: add LF after a previously unterminated row.

    A final CR is already a terminator. New rows preserve their supplied final
    terminator (including no terminator). No other lexical bytes change.
    """
    if len(csv_records(record)) != 1:
        raise Error("relational_insert_one_record_required")
    return existing + (b"\n" if existing and existing[-1:] not in (b"\r", b"\n") else b"") + record


def profile(data: bytes) -> dict:
    b = unpack(data)
    tables = []
    for t, (_, raw) in zip(b.schema["tables"], b.tables):
        rows = b.rows[t["name"]]
        terms = {"LF": 0, "CRLF": 0, "CR": 0, "none": 0}
        for r in rows:
            terms[{b"\n": "LF", b"\r\n": "CRLF", b"\r": "CR", b"": "none"}[r.terminator]] += 1
        tables.append({"name": t["name"], "rows": len(rows), "source_bytes": len(raw),
                       "null_cells": sum(c is None for r in rows for c in r.cells),
                       "empty_cells": sum(c == b"" for r in rows for c in r.cells),
                       "terminators": terms, "primary_key": t.get("primary_key", []),
                       "derived_columns": [c["name"] for c in t["columns"] if "derived" in c]})
    return {"format": "RLB1", "canonical_bytes": len(data), "schema_bytes": len(b.schema_bytes),
            "framing_bytes": len(data) - len(b.schema_bytes) - sum(len(v) for _, v in b.tables),
            "table_order": [n for n, _ in b.tables], "tables": tables,
            "equality": "Complete framed input bytes, not normalized logical rows"}
