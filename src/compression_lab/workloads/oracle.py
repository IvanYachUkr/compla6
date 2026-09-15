"""Independent evaluator-owned reference state and durable request/ack ledger.

This module never imports, executes, or borrows validation/serialization from a
candidate. The reference candidate is a separate standalone program. The oracle
retains lexical source bytes and uses the evaluator's RLB1 contract parser.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import time
from pathlib import Path

from ..util import Error
from . import relational
from .protocol import decode_blob, MAX_DECODED_BYTES

TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def require_movie_schema(schema: dict):
    layout = {
        "titles": [("id", "uint64", False), ("external_id", "text", False), ("title", "text", True),
                   ("year", "int64", True), ("original_title", "text", True)],
        "entities": [("id", "uint64", False), ("name", "text", True)],
        "episodes": [("id", "uint64", False), ("series_id", "uint64", False),
                     ("season", "int64", True), ("number", "int64", True)],
        "relationships": [("id", "uint64", False), ("title_id", "uint64", False),
                          ("entity_id", "uint64", False), ("role", "text", True)]}
    edges = {"titles": set(), "entities": set(), "episodes": {("id", "titles"), ("series_id", "titles")},
             "relationships": {("title_id", "titles"), ("entity_id", "entities")}}
    if [t["name"] for t in schema["tables"]] != list(layout):
        raise Error("mutable_movie_schema_required")
    for t in schema["tables"]:
        name = t["name"]
        if ([(c["name"], c["type"], c["nullable"]) for c in t["columns"]] != layout[name]
                or t.get("primary_key") != ["id"]):
            raise Error("mutable_movie_schema_required", name)
        actual = set()
        for fk in t.get("foreign_keys", []):
            if len(fk["columns"]) != 1 or fk["references"] != ["id"]:
                raise Error("mutable_movie_schema_required")
            actual.add((fk["columns"][0], fk["table"]))
        if actual != edges[name]:
            raise Error("mutable_movie_schema_required", name)
    columns = schema["tables"][0]["columns"]
    if (columns[1].get("derived") != {"kind": "prefix_id", "source": "id", "prefix": "tt", "width": 7}
            or columns[4].get("derived") != {"kind": "copy", "source": "title"}):
        raise Error("mutable_movie_schema_required", "derived title fields")


def transaction_identity(payload: dict) -> str:
    # The token is included. Whitespace/key order in the transport is not.
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()


def query_frame(records: list[tuple[str, bytes]]) -> bytes:
    total = 8
    for table, record in records:
        total += 10 + len(table.encode("utf-8")) + len(record)
        if total > MAX_DECODED_BYTES:
            raise Error("oracle_query_output_limit")
    result = bytearray(b"QRY1")
    result.extend(len(records).to_bytes(4, "little"))
    for table, record in records:
        name = table.encode("utf-8")
        result.extend(len(name).to_bytes(2, "little"))
        result.extend(name)
        result.extend(len(record).to_bytes(8, "little"))
        result.extend(record)
    return bytes(result)


class ReferenceState:
    def __init__(self, seed: bytes):
        self.bundle = relational.unpack(seed)
        require_movie_schema(self.bundle.schema)
        self.head = self.generation = self.checkpoint = 0
        self.receipts: dict[str, dict] = {}

    def prepare(self, payload: dict) -> relational.Bundle:
        if (not isinstance(payload, dict) or set(payload) != {"token", "records"}
                or not isinstance(payload["token"], str) or not TOKEN.fullmatch(payload["token"])
                or not isinstance(payload["records"], list) or not 1 <= len(payload["records"]) <= 128):
            raise Error("oracle_invalid_transaction")
        identity = transaction_identity(payload)
        previous = self.receipts.get(payload["token"])
        if previous is not None:
            if previous["digest"] != identity:
                raise Error("oracle_token_conflict")
            return self.bundle
        if self.head >= (1 << 64) - 1:
            raise Error("oracle_transaction_id_exhausted")
        tables = dict(self.bundle.tables)
        total = 0
        for row in payload["records"]:
            if (not isinstance(row, dict) or set(row) != {"table", "record_b64"}
                    or not isinstance(row["table"], str) or row["table"] not in tables):
                raise Error("oracle_invalid_insert")
            raw = decode_blob(row["record_b64"], 256 * 1024)
            total += len(raw)
            if total > 512 * 1024:
                raise Error("oracle_transaction_limit")
            tables[row["table"]] = relational.append_record(tables[row["table"]], raw)
        updated = relational.pack(self.bundle.schema_bytes, [(name, tables[name]) for name, _ in self.bundle.tables])
        if len(updated) > 5 * 1024 * 1024:
            raise Error("oracle_store_contract_limit")
        return relational.unpack(updated)

    def _metadata(self, meta: dict, wanted_head: int, pending=0):
        for field in ("head", "generation", "checkpoint"):
            if type(meta.get(field)) is not int or not 0 <= meta[field] < (1 << 64):
                raise Error("mutable_invalid_metadata", field)
        if not wanted_head <= meta["head"] <= wanted_head + pending:
            raise Error("mutable_acknowledged_head_lost_or_unexpected", str((wanted_head, meta["head"])))
        if meta["generation"] < self.generation:
            raise Error("mutable_generation_regressed")
        if meta["checkpoint"] < self.checkpoint or meta["checkpoint"] > meta["head"]:
            raise Error("mutable_checkpoint_regressed_or_invalid")

    def observe(self, meta: dict, pending=0):
        self._metadata(meta, self.head, pending)
        self.generation, self.checkpoint = meta["generation"], meta["checkpoint"]

    def accept(self, payload: dict, receipt: dict, *, resolving=False, pending_after=0):
        staged = self.prepare(payload)
        old = self.receipts.get(payload["token"])
        expected_id = old["txid"] if old else self.head + 1
        if type(receipt.get("txid")) is not int or receipt["txid"] != expected_id:
            raise Error("mutable_transaction_id_not_monotonic", str((expected_id, receipt.get("txid"))))
        if type(receipt.get("replayed")) is not bool:
            raise Error("mutable_invalid_replay_flag")
        if old is not None and receipt["replayed"] is not True:
            raise Error("mutable_token_replay_not_idempotent")
        if old is None and not resolving and receipt["replayed"]:
            raise Error("mutable_unrequested_token_replay")
        head = self.head if old else self.head + 1
        self._metadata(receipt, head, pending_after)
        if old is None:
            self.bundle = staged
            self.head = head
            self.receipts[payload["token"]] = {"txid": expected_id, "digest": transaction_identity(payload)}
        self.generation, self.checkpoint = receipt["generation"], receipt["checkpoint"]

    def export_bytes(self) -> bytes:
        return relational.pack(self.bundle.schema_bytes, self.bundle.tables)

    def point(self, table: str, key: int) -> bytes:
        if table not in self.bundle.rows or type(key) is not int or not 0 <= key < (1 << 64):
            raise Error("oracle_invalid_query")
        records = [(table, row.raw) for row in self.bundle.rows[table] if row.values[0] == key]
        return query_frame(records)

    def join(self, kind: str, key: int) -> bytes:
        if kind not in ("title_entities", "episodes") or type(key) is not int or not 0 <= key < (1 << 64):
            raise Error("oracle_invalid_query")
        titles = {row.values[0]: row for row in self.bundle.rows["titles"]}
        if key not in titles:
            return query_frame([])
        rows = [("titles", titles[key].raw)]
        if kind == "title_entities":
            people = {row.values[0]: row for row in self.bundle.rows["entities"]}
            for relation in self.bundle.rows["relationships"]:
                if relation.values[1] == key:
                    rows.extend((("relationships", relation.raw), ("entities", people[relation.values[2]].raw)))
        else:
            for episode in self.bundle.rows["episodes"]:
                if episode.values[1] == key:
                    rows.extend((("episodes", episode.raw), ("titles", titles[episode.values[0]].raw)))
        return query_frame(rows)


class RequestLedger:
    """Append/fsync before sending; append/fsync after receiving.

    Evidence retains unacknowledged requests. A process crash can commit an
    unacknowledged operation; the evaluator resolves it by replaying its token.
    The ledger is not a file in the candidate's writable store.
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fsync(fd)
            os.close(fd)
            d = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(d)
            os.close(d)
        events = self.verify()
        self.sequence = len(events)
        self.previous = events[-1]["hash"] if events else "0" * 64

    @staticmethod
    def _serialize(value) -> bytes:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")

    def _append(self, event: dict) -> int:
        event = {"event_id": self.sequence + 1, "previous": self.previous,
                 "monotonic_ns": time.monotonic_ns(), **event}
        event["hash"] = hashlib.sha256(self._serialize(event)).hexdigest()
        data = self._serialize(event) + b"\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise Error("mutable_unsafe_ack_ledger")
            offset = 0
            while offset < len(data):
                n = os.write(fd, data[offset:])
                if n <= 0:
                    raise Error("mutable_ack_ledger_short_write")
                offset += n
            os.fsync(fd)
        finally:
            os.close(fd)
        self.sequence, self.previous = event["event_id"], event["hash"]
        return self.sequence

    def request(self, session_id: str, operation: str, args: dict) -> int:
        return self._append({"kind": "request", "session_id": session_id, "op": operation, "args": args})

    def response(self, request_event: int, response: dict) -> int:
        return self._append({"kind": "response", "request_event_id": request_event, "response": response})

    def note(self, name: str, details: dict) -> int:
        return self._append({"kind": "note", "name": name, "details": details})

    def verify(self) -> list[dict]:
        data = self.path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise Error("mutable_ack_ledger_torn_tail", "Preserved; not silently rewritten")
        events, previous = [], "0" * 64
        requests, replies = set(), set()
        for line in data.splitlines():
            try:
                event = json.loads(line)
                checksum = event["hash"]
                unsigned = {k: v for k, v in event.items() if k != "hash"}
                valid = (event["event_id"] == len(events) + 1 and event["previous"] == previous
                         and hashlib.sha256(self._serialize(unsigned)).hexdigest() == checksum)
            except (ValueError, TypeError, KeyError) as exc:
                raise Error("mutable_ack_ledger_corrupt") from exc
            if not valid:
                raise Error("mutable_ack_ledger_corrupt")
            if event["kind"] == "request":
                requests.add(event["event_id"])
            elif event["kind"] == "response":
                target = event["request_event_id"]
                if target not in requests or target in replies:
                    raise Error("mutable_ack_ledger_invalid_receipt")
                replies.add(target)
            elif event["kind"] != "note":
                raise Error("mutable_ack_ledger_unknown_event")
            events.append(event)
            previous = checksum
        return events

    def pending(self) -> list[dict]:
        events = self.verify()
        replied = {e["request_event_id"] for e in events if e["kind"] == "response"}
        return [e for e in events if e["kind"] == "request" and e["event_id"] not in replied]

    def acknowledged(self) -> list[dict]:
        events = self.verify()
        requests = {e["event_id"]: e for e in events if e["kind"] == "request"}
        return [e for e in events if e["kind"] == "response" and e["response"]["ok"]
                and requests[e["request_event_id"]]["op"] == "transaction"]
