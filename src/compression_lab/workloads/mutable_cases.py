"""Evaluator-owned relational transaction, recovery and metric sequences."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

from ..util import Error, canonical, save, sha
from . import metrics, protocol
from .fixtures import row
from .cards import sequence_name
from .oracle import ReferenceState, RequestLedger
from .session import Session

SCENARIOS = (
    "basic", "merge_reopen_insert", "kill_idle", "kill_merge_before_manifest", "kill_merge_after_manifest",
    "kill_wal_short_write", "kill_wal_after_fsync", "io_wal_short_write", "io_wal_fsync",
    "io_manifest_rename", "io_merge_snapshot_fsync", "io_directory_fsync",
)
REQUIRED_CAPABILITIES = {"single_writer", "insert_only", "atomic_bundles", "episodes", "relationships",
                         "foreign_keys", "idempotent_tokens", "point", "join", "merge", "canonical_export",
                         "process_crash_only"}


class StoreCase:
    def __init__(self, candidate: Path, seed: bytes, output: Path, *, scenario="basic", limits=None,
                 cancel=None, deadline=None, object_alias="synthetic", metric_trial=None, runtime_override=None,
                 diagnostic=False, measurement_sequence=None):
        self.candidate, self.seed, self.out = Path(candidate), seed, Path(output)
        self.out.mkdir(parents=True, exist_ok=False)
        self.store = self.out / "store"
        self.scenario, self.object_alias, self.metric_trial = scenario, object_alias, metric_trial
        self.limits = limits or {"timeout_seconds": 20, "memory_bytes": 1024**3, "output_bytes": 128 * 1024 * 1024}
        self.cancel, self.deadline = cancel, deadline
        self.runtime_override, self.diagnostic = runtime_override, diagnostic
        self.state = ReferenceState(seed)
        self._used = {name: {r.values[0] for r in rows} for name, rows in self.state.bundle.rows.items()}
        self._payloads = {}
        self.measurement_sequence = measurement_sequence or {"transactions": 2, "merges": 2}
        self.ledger = RequestLedger(self.out / "ledger.jsonl")
        self.p = None
        self.sessions, self.operations, self.events = [], [], []
        self.session_count, self._seen = 0, 0
        self.phase = "measurement" if scenario == "measurement" else "diagnostic" if diagnostic else "correctness"
        self.tags = {"measurement_sequence": sequence_name(self.measurement_sequence)} if scenario == "measurement" else {}
        self.logical_insert_growth = 0
        self.measured_store = None

    def _flush_operations(self):
        if self.p is None:
            return
        while self._seen < len(self.p.operations):
            operation = {**self.p.operations[self._seen], "session_id": self.session_id,
                         "scenario": self.scenario, "object_alias": self.object_alias,
                         "metric_trial": self.metric_trial, **self.tags}
            self._seen += 1
            self.operations.append(operation)
            with (self.out / "operations.jsonl").open("ab") as stream:
                stream.write(canonical(operation) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())

    def _call(self, op: str, args: dict) -> dict:
        if self.p is None:
            raise Error("mutable_no_active_session")
        request_event = self.ledger.request(self.session_id, op, args)
        try:
            response = self.p.request(op, args)
            self.ledger.response(request_event, response)
            self._flush_operations()
            return response
        except BaseException as exc:
            self._flush_operations()
            self.ledger.note("transport_failure", {"request_event_id": request_event,
                                                   "reason_code": getattr(exc, "code", "transport_error")})
            raise

    def rpc(self, op: str, args=None) -> dict:
        response = self._call(op, {} if args is None else args)
        if not response["ok"]:
            raise Error("mutable_candidate_rejected", op + ": " + str(response["error"]))
        return response["result"]

    def start(self, *, build=False, pending=0):
        if self.p is not None:
            raise Error("mutable_case_session_overlap")
        self.session_count += 1
        self.session_id = self.scenario + "/session-" + str(self.session_count)
        self._seen = 0
        self.p = Session(self.candidate, self.store, timeout=self.limits["timeout_seconds"],
                         memory=self.limits["memory_bytes"], output_limit=self.limits.get("output_bytes", 128 * 1024 * 1024),
                         cancel=self.cancel, deadline=self.deadline, phase=self.phase,
                         cache="fresh_process_no_os_cache_drop", runtime_override=self.runtime_override,
                         diagnostic=self.diagnostic)
        hello = self.rpc("hello")
        if (hello.get("protocol_version") != 1 or not isinstance(hello.get("capabilities"), list)
                or not REQUIRED_CAPABILITIES <= set(hello["capabilities"])
                or hello.get("power_loss_certified") is not False
                or hello.get("max_request_bytes") != protocol.MAX_REQUEST_BYTES
                or hello.get("max_response_bytes") != protocol.MAX_RESPONSE_BYTES):
            raise Error("mutable_protocol_capabilities")
        self.fault_points = set(hello.get("fault_points", []))
        meta = self.rpc("build", {"bundle_b64": base64.b64encode(self.seed).decode("ascii")}) if build else self.rpc("open")
        self.state.observe(meta, pending=pending)
        self.p.cache = "warm_process"
        return meta

    def stop(self):
        if self.p is None:
            return
        p = self.p
        try:
            if not p._closed:
                request_event = self.ledger.request(self.session_id, "close", {})
                response = p.close()
                self.ledger.response(request_event, response)
            self._flush_operations()
        finally:
            if not p._closed:
                p.kill("case_cleanup")
                self._flush_operations()
            self.sessions.append({"session_id": self.session_id, **p.summary()})
            self.p = None

    def kill(self, point=None):
        if self.p is None:
            raise Error("mutable_no_active_session")
        self.ledger.note("external_process_kill", {"signal": "SIGKILL", "signal_number": 9,
                         "hook_point": point, "host_pid": self.p.pid,
                         "acknowledged_unique_tokens": len(self.state.receipts), "head": self.state.head})
        self.p.kill("external_SIGKILL")
        self._flush_operations()
        self.sessions.append({"session_id": self.session_id, **self.p.summary()})
        self.p = None
        self.events.append({"event": "external_SIGKILL", "point": point})

    def transaction(self, payload: dict, *, resolving=False):
        before = len(self.state.export_bytes())
        self.state.prepare(payload)
        result = self.rpc("transaction", payload)
        self.state.accept(payload, result, resolving=resolving)
        self.logical_insert_growth += len(self.state.export_bytes()) - before
        return result

    def unused_id(self, table, reserve=False):
        value = 0
        while value in self._used[table]:
            value += 1
        if value >= (1 << 64):
            raise Error("mutable_fixture_id_space_exhausted")
        if reserve:
            self._used[table].add(value)
        return value

    def payload(self, index, kind="bundle"):
        """Allocate from the actual seed; replay preserves exact request bytes."""
        key = index, kind
        if key in self._payloads:
            return self._payloads[key]
        relation = self.unused_id("relationships", True)
        if kind == "relationship":
            title = self.state.bundle.rows["titles"][0].values[0]
            entity = self.state.bundle.rows["entities"][0].values[0]
            records = [row("relationships", f'{relation},{title},{entity},new relation\r\n'.encode())]
        else:
            title = self.unused_id("titles", True)
            self._used["episodes"].add(title)
            entity = self.unused_id("entities", True)
            series = self.state.bundle.rows["titles"][0].values[0] if self.state.bundle.rows["titles"] else title
            name = f'"Inserted {index}, with ""quote"""'
            records = [row("titles", f'{title},tt{title:07d},{name},+002026,{name}\r\n'.encode()),
                       row("entities", f'{entity},"Entity {index}"\n'.encode()),
                       row("episodes", f'{title},{series},1,{index}\r'.encode()),
                       row("relationships", f'{relation},{title},{entity},actor\n'.encode())]
        payload = {"token": f"evaluator-{kind}-{index}", "records": records}
        self._payloads[key] = payload
        return payload

    def merge(self, operation="merge"):
        generation = self.state.generation
        result = self.rpc(operation)
        self.state.observe(result)
        if result["generation"] <= generation or result["checkpoint"] != self.state.head:
            raise Error("mutable_merge_checkpoint_invalid")
        self.events.append({"event": operation, **result})
        return result

    def export_check(self, code="mutable_export_bytes_mismatch"):
        result = self.rpc("export")
        raw = protocol.decode_blob(result.get("bytes_b64"))
        expected = self.state.export_bytes()
        if raw != expected:
            (self.out / "expected.rlb").write_bytes(expected)
            (self.out / "actual.rlb").write_bytes(raw)
            raise Error(code)
        if type(result.get("bytes")) is not int or result["bytes"] != len(raw) or result.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise Error("mutable_export_metadata_mismatch")
        (self.out / "expected.rlb").write_bytes(expected)
        (self.out / "actual.rlb").write_bytes(raw)
        return raw

    def query_check(self, operation: str, args: dict, expected=None):
        if expected is None:
            try:
                expected = self.state.point(args["table"], args["id"]) if operation == "point" else self.state.join(args["kind"], args["id"])
            except Error as exc:
                if exc.code != "oracle_query_output_limit":
                    raise
                response = self._call(operation, args)
                if response["ok"] or response["error"]["code"] != "query_output_limit":
                    raise Error("mutable_query_limit_not_refused")
                self.export_check()
                self.state.observe(self.rpc("stats"))
                return
        response = self.rpc(operation, args)
        raw = protocol.decode_blob(response.get("bytes_b64"))
        if raw != expected:
            (self.out / "query-expected.bin").write_bytes(expected)
            (self.out / "query-actual.bin").write_bytes(raw)
            save(self.out / "query-request.json", {"op": operation, "args": args})
            raise Error("mutable_query_bytes_mismatch", operation + ": " + str(args))
        count = int.from_bytes(expected[4:8], "little")
        if type(response.get("rows")) is not int or response["rows"] != count:
            raise Error("mutable_query_row_count_mismatch")

    def verify_all(self):
        self.export_check()
        stats = self.rpc("stats")
        self.state.observe(stats)
        if stats.get("recovery_required") is not False or stats.get("receipt_count") != len(self.state.receipts):
            raise Error("mutable_receipt_inventory_mismatch")
        for table, rows in self.state.bundle.rows.items():
            keys = [r.values[0] for r in rows] + [self.unused_id(table)]
            for key in keys:
                self.query_check("point", {"table": table, "id": key})
        for key in [r.values[0] for r in self.state.bundle.rows["titles"]] + [self.unused_id("titles")]:
            for kind in ("title_entities", "episodes"):
                self.query_check("join", {"kind": kind, "id": key})
        return stats

    def reject(self, op: str, args: dict):
        before = self.state.export_bytes()
        response = self._call(op, args)
        if response["ok"]:
            raise Error("mutable_invalid_operation_acknowledged", op)
        # The complete canonical database, not just counts, must be unchanged.
        actual = protocol.decode_blob(self.rpc("export").get("bytes_b64"))
        if actual != before:
            (self.out / "expected.rlb").write_bytes(before)
            (self.out / "actual.rlb").write_bytes(actual)
            raise Error("mutable_rejected_transaction_changed_bytes", op)
        self.state.observe(self.rpc("stats"))

    def arm(self, point: str, mode: str):
        if point not in self.fault_points:
            raise Error("mutable_required_fault_hook_unavailable", point)
        self.rpc("fault", {"point": point, "mode": mode})

    def kill_inflight(self, operation: str, args: dict, point: str):
        self.arm(point, "pause")
        request_event = self.ledger.request(self.session_id, operation, args)
        self.p.begin(operation, args)
        end = min(self.deadline or float("inf"), time.monotonic() + self.limits["timeout_seconds"])
        marker = self.store / "fault.ready"
        while not marker.exists():
            self.p._check(end)
            # Read EOF/errors, but do not mistake a completed response for a kill
            # during the named merge phase.
            if self.p._read_ready(.005) or self.p._buffer:
                raise Error("mutable_fault_pause_not_reached", point)
        contents = json.loads(marker.read_bytes())
        if contents.get("point") != point:
            raise Error("mutable_stale_fault_marker")
        self.ledger.note("fault_pause_observed", {"request_event_id": request_event, "marker": contents})
        self.kill(point)

    def basic(self):
        transaction = self.payload
        self.start(build=True)
        self.verify_all()
        self.transaction(transaction(1))
        self.transaction(transaction(2, "relationship"))
        self.transaction(transaction(1))
        self.reject("transaction", {**transaction(3), "token": transaction(1)["token"]})
        fresh_entity, fresh_relation, absent_title = self.unused_id("entities"), self.unused_id("relationships"), self.unused_id("titles")
        duplicate_entity = self.state.bundle.rows["entities"][0].values[0]
        self.reject("transaction", {"token": "reject-atomic-bundle", "records": [
            row("entities", f"{fresh_entity},new entity\n".encode()),
            row("relationships", f"{fresh_relation},{absent_title},{fresh_entity},orphan\n".encode())]})
        self.reject("transaction", {"token": "reject-duplicate-normalized-id", "records": [
            row("entities", f"+000{duplicate_entity},duplicate\r\n".encode())]})
        self.reject("transaction", {"token": "reject-integer-boundary", "records": [
            row("titles", f"{absent_title},tt{absent_title:07d},overflow,9223372036854775808,overflow\n".encode())]})
        for op in ("update", "delete", "concurrent_writer"):
            self.reject(op, {})
        self.reject("point", {"table": "titles", "id": True})
        self.reject("point", {"table": "../titles", "id": 7})
        self.verify_all()
        self.merge("checkpoint")
        self.stop()
        self.start()
        self.verify_all()

    def merge_reopen_insert(self):
        transaction = self.payload
        self.start(build=True)
        self.transaction(transaction(1))
        self.merge()
        self.stop()
        self.start()
        self.transaction(transaction(2))
        self.stop()
        self.start()
        self.verify_all()
        self.transaction(transaction(1))
        self.verify_all()

    def killed(self, point):
        transaction = self.payload
        self.start(build=True)
        self.transaction(transaction(1))
        if point == "idle":
            self.kill()
        elif point.startswith("merge_"):
            self.kill_inflight("merge", {}, point)
        else:
            pending = transaction(2)
            self.state.prepare(pending)
            self.kill_inflight("transaction", pending, point)
        self.start(pending=1 if point.startswith("wal_") else 0)
        if point.startswith("wal_"):
            self.transaction(transaction(2), resolving=True)
        self.verify_all()
        self.transaction(transaction(3))
        self.merge()
        self.stop()
        self.start()
        self.verify_all()
        # Replay receipts originating before every previous merge/recovery.
        for payload in (transaction(1), transaction(3)):
            self.transaction(payload)
        self.verify_all()

    def wal_failure(self, point):
        transaction = self.payload
        self.start(build=True)
        self.transaction(transaction(1))
        failed, continuation = transaction(2), transaction(3)
        self.arm(point, "error")
        response = self._call("transaction", failed)
        if response["ok"]:
            raise Error("mutable_io_fault_not_refused")
        later = self._call("transaction", continuation)
        if later["ok"]:
            receipt = later["result"]
            if receipt.get("txid") == self.state.head + 2:
                # Safe continuation may have committed the unacknowledged first
                # operation. Obtain its actual replay receipt before adopting it.
                resolved = self.rpc("transaction", failed)
                before = len(self.state.export_bytes())
                self.state.accept(failed, resolved, resolving=True, pending_after=1)
                self.logical_insert_growth += len(self.state.export_bytes()) - before
            before = len(self.state.export_bytes())
            self.state.accept(continuation, receipt)
            self.logical_insert_growth += len(self.state.export_bytes()) - before
            self.events.append({"event": "io_safe_continuation_acknowledged", "point": point, "txid": receipt["txid"]})
        else:
            if later["error"]["code"] not in ("recovery_required", "io_error"):
                raise Error("mutable_io_refusal_not_explicit")
            self.events.append({"event": "io_refused_until_recovery", "point": point})
        self.kill()
        self.start(pending=0 if failed["token"] in self.state.receipts else 1)
        self.transaction(failed, resolving=True)
        self.verify_all()
        self.transaction(transaction(4))
        self.merge()
        self.stop()
        self.start()
        self.verify_all()

    def merge_failure(self, point):
        transaction = self.payload
        self.start(build=True)
        self.transaction(transaction(1))
        self.arm(point, "error")
        response = self._call("merge", {})
        if response["ok"]:
            raise Error("mutable_io_fault_not_refused")
        continuation = transaction(2)
        later = self._call("transaction", continuation)
        if later["ok"]:
            before = len(self.state.export_bytes())
            self.state.accept(continuation, later["result"])
            self.logical_insert_growth += len(self.state.export_bytes()) - before
            self.events.append({"event": "io_safe_continuation_acknowledged", "point": point})
        else:
            if later["error"]["code"] not in ("recovery_required", "io_error"):
                raise Error("mutable_io_refusal_not_explicit")
            self.events.append({"event": "io_refused_until_recovery", "point": point})
        self.kill()
        self.start()
        self.verify_all()
        self.transaction(continuation)
        self.transaction(transaction(3))
        self.merge()
        self.stop()
        self.start()
        self.verify_all()

    def measurement(self):
        sequence = self.measurement_sequence
        count, merges = sequence["transactions"], sequence["merges"]
        positions = {((i * count + merges - 1) // merges): i for i in range(1, merges + 1)}
        self.start(build=True)
        for index in range(1, count + 1):
            self.transaction(self.payload(index, "bundle" if index % 2 else "relationship"))
            title = self.state.bundle.rows["titles"][0].values[0]
            self.query_check("point", {"table": "titles", "id": title})
            self.query_check("join", {"kind": "title_entities", "id": title})
            if index in positions:
                self.tags["merge_round"] = positions[index]
                self.merge()
                self.tags.pop("merge_round")
        self.export_check()
        stats = self.rpc("stats")
        self.stop()
        self.start()
        self.query_check("point", {"table": "titles", "id": title})
        self.query_check("join", {"kind": "title_entities", "id": title})
        self.export_check()
        self.stop()
        self.measured_store = {"object_alias": self.object_alias, "trial": self.metric_trial,
                               "measurement_sequence": sequence_name(sequence),
                               "transaction_count": count, "merge_count": merges,
                               "canonical_export_bytes": len(self.state.export_bytes()),
                               "canonical_insert_growth_bytes": self.logical_insert_growth,
                               "store": metrics.space(self.store, hashes=True),
                               "candidate_receipt_report": {k: stats.get(k) for k in
                                  ("receipt_count", "receipt_payload_bytes", "receipt_storage", "index_storage")}}

    def run(self) -> dict:
        began = time.perf_counter_ns()
        raw = {"scenario": self.scenario, "object_alias": self.object_alias, "metric_trial": self.metric_trial,
               "quality_passed": False, "reason_codes": [], "power_loss_certified": False,
               "durability_scope": "Actual process SIGKILL and cooperative I/O fault hooks only; no power cut or storage reordering model"}
        try:
            if self.scenario == "basic":
                self.basic()
            elif self.scenario == "merge_reopen_insert":
                self.merge_reopen_insert()
            elif self.scenario == "measurement":
                self.measurement()
            elif self.scenario.startswith("kill_"):
                self.killed(self.scenario[5:])
            elif self.scenario.startswith("io_wal_"):
                self.wal_failure(self.scenario[3:])
            elif self.scenario.startswith("io_"):
                self.merge_failure(self.scenario[3:])
            else:
                raise Error("unknown_mutable_scenario")
            self.stop()
            self.ledger.verify()
            raw["quality_passed"] = True
        except BaseException as exc:
            raw.update(reason_codes=[getattr(exc, "code", "mutable_case_error")], error=str(exc)[:2000],
                       traceback=traceback.format_exc(limit=8))
            if self.p is not None:
                try:
                    self.kill("failed_case_cleanup")
                except BaseException as cleanup_error:
                    raw["cleanup_error"] = str(cleanup_error)[:1000]
        try:
            final_store = metrics.space(self.store, hashes=True)
        except BaseException as exc:
            final_store = {"inventory_error": getattr(exc, "code", "mutable_inventory_failed")}
            raw["quality_passed"] = False
            if final_store["inventory_error"] not in raw["reason_codes"]:
                raw["reason_codes"].append(final_store["inventory_error"])
        raw.update(elapsed_ns=time.perf_counter_ns() - began, sessions=self.sessions, operations=self.operations,
                   events=self.events, reference_head=self.state.head, reference_generation=self.state.generation,
                   reference_checkpoint=self.state.checkpoint, logical_insert_growth_bytes=self.logical_insert_growth,
                   measured_store=self.measured_store, store=final_store,
                   ledger={"path": "ledger.jsonl", "sha256": sha(self.out / "ledger.jsonl"),
                           "bytes": (self.out / "ledger.jsonl").stat().st_size,
                           "acknowledgments": len(self.ledger.acknowledged()),
                           "unanswered_requests_retained": len(self.ledger.pending())})
        save(self.out / "case.json", raw, 0o444)
        return raw
