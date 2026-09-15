"""Versioned public comparisons and explicitly untrusted agent interpretations.

This store is advisory memory, never an execution or policy authority. It reads
committed public evidence through Engine.raw and never starts an evaluation.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import math
from pathlib import Path

from . import dataset
from .util import Error, Ledger, canonical, digest, ident, lock, rel, safe, secure_directory, secure_load, secure_read
from .workloads.bridge import comparison_identity, rank_cost

MAX_LESSONS = 128
MAX_VERSIONS = 64
MAX_EVIDENCE = 8
MAX_RECORD_BYTES = 96 * 1024
MAX_JOURNAL_BYTES = MAX_VERSIONS * (MAX_RECORD_BYTES + 4096)
METRICS = {"package_bytes", "encode_bytes_per_second", "decode_bytes_per_second"}
ARTIFACT_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)
IDENTITY_FIELDS = {"run_id", "card_digest", "runtime_digest", "workload", "depth", "resource_profile",
                   "resource_profile_digest", "resource_profile_details", "resource_runtime_digest",
                   "timing_scope", "accounting_policy"}
NOTICE = ("Agent interpretations are unverified and untrusted. Structured support applies only to the "
          "specified public comparisons in this scope; it establishes neither causality nor held-out generalization. "
          "Procedure references grant no execution authority.")


class RefinementStore:
    """One bounded append-only Ledger per lesson; mutations serialize at the store.

    Identical proposals return the current version. Changed proposals create a
    new provisional version. rollback is an owner CLI operation, not an MCP tool.
    """

    def __init__(self, engine):
        if hasattr(engine, "researcher_view"): engine = engine.researcher_view()
        self.engine = engine
        self.root = Path(engine.root) / "refinements"
        with secure_directory(engine.root):
            self.root.mkdir(mode=0o700, exist_ok=True)
        with secure_directory(self.root):
            pass

    @contextlib.contextmanager
    def _locked(self):
        with secure_directory(self.root), lock(self.root / "operation.lock"):
            yield

    def _context(self):
        state = self.engine.state()
        card, pin = self.engine._profile_context()
        _, rows = self.engine.metadata()
        if any(row.get("split") not in dataset.public_partitions(card) for row in rows):
            raise Error("refinement_requires_public_context")
        resource = pin["resource_profile"]
        if resource["id"] != card["_primary_profile"]:
            raise Error("refinement_requires_primary_profile")
        fields = ("alias", "group", "split", "order", "canonical_bytes", "canonical_sha256")
        return {"run_id": state["run_id"], "card_digest": state["card_digest"],
                "evaluation_mode": card.get("evaluation_mode", "split"),
                "runtime_digest": state["runtime_digest"], "workload": card.get("workload", "independent_objects"),
                "depth": "full", "resource_profile": resource["id"],
                "resource_profile_digest": pin["resource_profile_digest"], "resource_profile_details": resource,
                "resource_runtime_digest": pin["runtime"]["runtime_digest"],
                "timing_scope": card.get("timing_policy", {}).get("scope", "train-plus-development-v1"),
                "accounting_policy": card.get("accounting_policy", "standalone-v1"),
                "data_context_digest": digest([{key: row.get(key) for key in fields} for row in rows])}

    @staticmethod
    def _ids(ids, exact=None):
        if not isinstance(ids, list) or not (len(ids) == exact if exact else 2 <= len(ids) <= MAX_EVIDENCE):
            raise Error("invalid_refinement_evidence")
        for result_id in ids:
            ident(result_id)
        if len(set(ids)) != len(ids):
            raise Error("invalid_refinement_evidence")
        return ids

    def _entry(self, entry):
        required = {"id", "kind", "interpretation", "evidence_result_ids", "claim"}
        if not isinstance(entry, dict) or not required <= entry.keys() or entry.keys() - required - {"procedure"}:
            raise Error("invalid_refinement_entry")
        ident(entry["id"])
        if entry["kind"] not in ("observation", "procedure"):
            raise Error("invalid_refinement_kind")
        text = entry["interpretation"]
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or "\0" in text:
            raise Error("invalid_refinement_interpretation")
        evidence = self._ids(entry["evidence_result_ids"])
        claim = entry["claim"]
        if not isinstance(claim, dict) or set(claim) != {"metric", "relation", "reference_result_id", "candidate_result_id"}:
            raise Error("invalid_refinement_claim")
        if not isinstance(claim["metric"], str) or claim["metric"] not in METRICS or claim["relation"] not in ("lower", "higher"):
            raise Error("invalid_refinement_claim")
        self._ids([claim["reference_result_id"], claim["candidate_result_id"]], exact=2)
        if not {claim["reference_result_id"], claim["candidate_result_id"]} <= set(evidence):
            raise Error("refinement_claim_missing_evidence")
        if (entry["kind"] == "procedure") != ("procedure" in entry):
            raise Error("invalid_refinement_procedure")
        if "procedure" in entry:
            procedure = entry["procedure"]
            if not isinstance(procedure, dict) or set(procedure) != {"candidate_digest", "path"}:
                raise Error("invalid_refinement_procedure")
            ident(procedure["candidate_digest"])
            rel(procedure["path"])
            if len(procedure["path"]) > 1024:
                raise Error("invalid_refinement_procedure")
        return copy.deepcopy(entry)

    def _evidence(self, ids, context, cache=None):
        cache = {} if cache is None else cache
        rows = {}
        for result_id in ids:
            if result_id not in cache:
                try:
                    raw = self.engine.raw(result_id)  # The sole authority for measurement values.
                    if not isinstance(raw, dict): raise Error("invalid_refinement_evidence")
                    cache[result_id] = raw
                except ARTIFACT_ERRORS as exc:
                    cache[result_id] = exc if isinstance(exc, Error) else Error("invalid_refinement_evidence")
            raw = cache[result_id]
            if isinstance(raw, Error): raise raw
            if (raw.get("data_scope", "public") != "public" or "private_attempt" in raw
                    or raw.get("quality_passed") is not True or raw.get("status") not in ("eligible", "ineligible")):
                raise Error("refinement_requires_public_full_quality")
            if (not IDENTITY_FIELDS <= raw.keys() or raw.get("run_id") != context["run_id"]
                    or comparison_identity(raw) != comparison_identity(context)
                    or raw.get("resource_profile_details") != context["resource_profile_details"]):
                raise Error("refinement_context_mismatch")
            rows[result_id] = raw
        return rows

    @staticmethod
    def _observe(claim, ids, rows):
        def value(raw):
            metric = claim["metric"]
            measured = rank_cost(raw) if metric == "package_bytes" else raw.get("timing", {}).get(
                "encode" if metric.startswith("encode") else "decode", {}).get("median_bytes_per_second")
            if (type(measured) not in (int, float) or measured < 0
                    or (isinstance(measured, float) and not math.isfinite(measured))):
                raise Error("invalid_refinement_metric")
            return measured
        reference, candidate = (value(rows[result_id]) for result_id in ids)
        return {"metric": claim["metric"], "relation": claim["relation"],
                "reference_result_id": ids[0], "candidate_result_id": ids[1],
                "reference_value": reference, "candidate_value": candidate, "difference": candidate - reference,
                "supports_claim": candidate < reference if claim["relation"] == "lower" else candidate > reference}

    def _procedure(self, procedure, candidate_digest):
        cid, name = procedure["candidate_digest"], procedure["path"]
        if cid != candidate_digest or cid not in self.engine.state()["registered"]:
            raise Error("refinement_procedure_candidate_mismatch")
        root = Path(self.engine.root) / "candidates" / cid
        try:
            registration = secure_load(root, "registration.json", 4 * 1024 * 1024)
            if (registration.get("candidate_digest") != cid
                    or digest({k: v for k, v in registration.items() if k != "candidate_digest"}) != cid):
                raise Error("stale_candidate_digest")
            records = registration["source_files"]
            if not isinstance(records, list) or len(records) > 20000 or digest(records) != registration["source_digest"]:
                raise Error("stale_source_digest")
            inventory = {record["path"]: record for record in records}
            if len(inventory) != len(records):
                raise Error("stale_source_digest")
            manifest_name = "store_candidate.json" if registration.get("workload") == "mutable_store" else "candidate.json"
            def checked_read(path, limit):
                record = inventory.get(path)
                if not record:
                    raise Error("refinement_procedure_not_source")
                content = secure_read(root / "source", path, limit)
                if len(content) != record["bytes"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                    raise Error("stale_source_digest")
                return content
            manifest = json.loads(checked_read(manifest_name, 1024 * 1024))
            if name not in manifest.get("source_paths", []):
                raise Error("refinement_procedure_not_source")
            checked_read(name, 2 * 1024 * 1024)
            record = inventory[name]
            return {"candidate_digest": cid, "path": name, "sha256": record["sha256"], "bytes": record["bytes"],
                    "source_digest": registration["source_digest"], "use": "readable_reference_only"}
        except (OSError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, Error):
                raise
            raise Error("invalid_refinement_procedure") from exc

    def _ledger(self, lesson_id, required=True):
        ident(lesson_id)
        root = self.root / lesson_id
        if root.is_symlink():
            raise Error("unsafe_refinement_store")
        if not root.exists():
            if required:
                raise Error("unknown_refinement")
            return Ledger(root)
        with secure_directory(root):
            pass
        ledger = Ledger(root)
        if ledger.journal.is_symlink():
            raise Error("unsafe_refinement_store")
        if ledger.journal.exists():
            if safe(root, "events.jsonl").stat().st_size > MAX_JOURNAL_BYTES:
                raise Error("refinement_history_limit")
        elif required:
            raise Error("unknown_refinement")
        return ledger

    def _names(self):
        names = []
        for path in self.root.iterdir():
            if path.name == "operation.lock":
                continue
            ident(path.name)
            if path.is_symlink() or not path.is_dir():
                raise Error("unsafe_refinement_store")
            if (path / "events.jsonl").exists():
                names.append(path.name)
            if len(names) > MAX_LESSONS:
                raise Error("refinement_store_limit")
        return sorted(names)

    def _history(self, ledger):
        # Ledger.read has already checked its hash chain and repaired a partial tail.
        return [json.loads(line)["state"] for line in secure_read(ledger.root, "events.jsonl", MAX_JOURNAL_BYTES).splitlines()]

    def _write(self, ledger, record, previous, operation):
        version = previous["version"] + 1 if previous else 1
        if version > MAX_VERSIONS:
            raise Error("refinement_version_limit")
        record = {**record, "schema_version": 1, "stage": "public_search", "version": version,
                  "previous_content_digest": previous["content_digest"] if previous else None,
                  "operation": operation}
        record.pop("content_digest", None)
        if operation != "rollback":
            record.pop("restored_from_version", None)
        record["content_digest"] = digest(record)
        if len(canonical(record)) > MAX_RECORD_BYTES:
            raise Error("refinement_record_limit")
        if self._context() != record["scope"]:
            raise Error("refinement_context_mismatch")
        if previous:
            ledger.update(operation, lambda state: record)
        else:
            ledger.create(record)
        return copy.deepcopy(record)

    def _validate_current(self, entry, context, evidence_cache=None):
        if entry["scope"] != context:
            raise Error("refinement_context_mismatch")
        if (entry.get("trusted_policy") is not False
                or entry.get("interpretation_status") != "unverified_agent_interpretation"
                or digest({key: value for key, value in entry.items() if key != "content_digest"}) != entry.get("content_digest")):
            raise Error("invalid_refinement_record")
        observed = entry["observed"]
        pair = [observed["reference_result_id"], observed["candidate_result_id"]]
        ids = list(dict.fromkeys(entry["evidence_result_ids"] + pair))
        rows = self._evidence(ids, context, evidence_cache)
        if (self._observe(entry["claim"], pair, rows) != observed
                or [rows[rid]["candidate_digest"] for rid in pair] != [entry["reference_candidate_digest"], entry["candidate_digest"]]):
            raise Error("stale_refinement_observation")
        if "procedure" in entry:
            checked = self._procedure(entry["procedure"], entry["candidate_digest"])
            if checked != entry["procedure"]:
                raise Error("stale_source_digest")

    def propose(self, entry: dict) -> dict:
        entry = self._entry(entry)
        with self._locked():
            context = self._context()
            rows = self._evidence(entry["evidence_result_ids"], context)
            claim = entry["claim"]
            ids = [claim["reference_result_id"], claim["candidate_result_id"]]
            record = {**entry, "proposal_digest": digest(entry), "scope": context, "status": "provisional",
                      "interpretation_status": "unverified_agent_interpretation", "trusted_policy": False,
                      "reference_candidate_digest": rows[ids[0]]["candidate_digest"],
                      "candidate_digest": rows[ids[1]]["candidate_digest"], "observed": self._observe(claim, ids, rows)}
            if "procedure" in entry:
                record["procedure"] = self._procedure(entry["procedure"], record["candidate_digest"])
            ledger = self._ledger(entry["id"], required=False)
            previous = ledger.read() if ledger.journal.exists() else None
            if previous and previous["scope"] == context and previous["proposal_digest"] == record["proposal_digest"]:
                self._validate_current(previous, context)
                return copy.deepcopy(previous)
            if (previous and previous["scope"] == context and previous["status"] == "refuted"
                    and previous["claim"] == entry["claim"]):
                raise Error("refinement_refuted_claim_unchanged")
            if not previous and len(self._names()) >= MAX_LESSONS:
                raise Error("refinement_store_limit")
            return self._write(ledger, record, previous, "propose")

    def check(self, lesson_id: str, result_ids: list[str]) -> dict:
        self._ids(result_ids, exact=2)
        with self._locked():
            context = self._context()
            ledger = self._ledger(lesson_id)
            previous = ledger.read()
            self._validate_current(previous, context)
            if previous["status"] == "refuted":
                raise Error("refinement_refuted")
            used = set()
            for entry in self._history(ledger):
                used.update(entry["evidence_result_ids"])
                used.update(entry["observed"][key] for key in ("reference_result_id", "candidate_result_id"))
            if used.intersection(result_ids):
                raise Error("refinement_evidence_not_fresh")
            rows = self._evidence(result_ids, context)
            if [rows[rid]["candidate_digest"] for rid in result_ids] != [previous["reference_candidate_digest"], previous["candidate_digest"]]:
                raise Error("refinement_candidate_pair_mismatch")
            observed = self._observe(previous["claim"], result_ids, rows)
            record = {**previous, "observed": observed,
                      "status": "supported_within_scope" if observed["supports_claim"] else "refuted"}
            return self._write(ledger, record, previous, "check")

    def _collect(self):
        try:
            context = self._context()
        except ARTIFACT_ERRORS as exc:
            return None, [], {"context_unavailable": getattr(exc, "code", "invalid_refinement_context")}
        entries, excluded = [], {"expired": 0, "refuted": 0, "unsupported": 0}
        evidence_cache = {}  # Re-read on the next list/snapshot; never cache across requests.
        for name in self._names():
            try:
                entry = self._ledger(name).read()
                if entry["status"] == "refuted":
                    excluded["refuted"] += 1
                    continue
                if not entry["observed"]["supports_claim"]:
                    excluded["unsupported"] += 1
                    continue
                self._validate_current(entry, context, evidence_cache)
            except ARTIFACT_ERRORS as exc:
                excluded["expired"] += 1
                excluded.setdefault("errors", {})[name] = getattr(exc, "code", "invalid_refinement_record")
                continue
            entries.append(entry)
        return context, entries, excluded

    def list(self, limit: int = 8) -> dict:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise Error("invalid_refinement_limit")
        with self._locked():
            context, entries, excluded = self._collect()
            keys = ("id", "kind", "version", "content_digest", "status", "interpretation_status", "trusted_policy",
                    "evidence_result_ids", "claim", "observed", "procedure")
            compact = [{**{key: entry[key] for key in keys if key in entry},
                        "interpretation": entry["interpretation"][:1200],
                        "interpretation_truncated": len(entry["interpretation"]) > 1200} for entry in entries[:limit]]
            return {"schema_version": 1, "context": context, "entries": compact, "excluded": excluded, "notice": NOTICE}

    def rollback(self, lesson_id: str, version: int) -> dict:
        """Owner-only wiring must enforce authority; history is restored as a new version."""
        if type(version) is not int or version < 1:
            raise Error("invalid_refinement_version")
        with self._locked():
            context = self._context()
            ledger = self._ledger(lesson_id)
            previous = ledger.read()
            if previous["scope"] != context:
                raise Error("refinement_context_mismatch")
            restored = next((entry for entry in self._history(ledger) if entry["version"] == version), None)
            if restored is None:
                raise Error("unknown_refinement_version")
            self._validate_current(restored, context)
            return self._write(ledger, {**restored, "restored_from_version": version}, previous, "rollback")

    def snapshot(self) -> dict:
        with self._locked():
            context, entries, _ = self._collect()
            snapshot = {"schema_version": 1, "context": context,
                        "entry_versions": {entry["id"]: entry["version"] for entry in entries},
                        "entry_digests": {entry["id"]: entry["content_digest"] for entry in entries}}
            return {**snapshot, "digest": digest(snapshot)}
