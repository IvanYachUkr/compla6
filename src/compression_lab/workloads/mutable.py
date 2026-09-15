"""Mutable-workload gates underneath the existing durable job lifecycle.

This evaluator validates a bounded movie-store contract. It does not certify
power-loss behavior, claim cold OS caches, or apply a static encoding floor.
"""
from __future__ import annotations

import statistics
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

from .. import candidate as native, runner
from ..util import Error, canonical, save, sha
from . import metrics, registry
from .cards import validate_selection, measurement_sequences, sequence_name
from .mutable_cases import SCENARIOS, StoreCase
from .relational import profile


def summarize(cases: list[dict], fixed: dict, rows: list[dict]) -> dict:
    """All raw trials stay in cases; only clean measurement phases are pooled."""
    operations = [operation for case in cases for operation in case.get("operations", [])]
    sessions = [session for case in cases for session in case.get("sessions", [])]
    measured = [case["measured_store"] for case in cases if case.get("measured_store")]
    by_alias = {}
    writes = []
    for row in rows:
        values = [trial for trial in measured if trial["object_alias"] == row["alias"]]
        if values:
            per_sequence = {}
            for trial in values:
                name = trial.get("measurement_sequence", "inserts-2-merges-2")
                per_sequence.setdefault(name, []).append(trial)
            summaries = {}
            for name, trials in per_sequence.items():
                logical = statistics.median(trial["store"]["logical_bytes"] for trial in trials)
                summaries[name] = {"transactions": trials[0].get("transaction_count", 2),
                    "merges": trials[0].get("merge_count", 2), "trials": trials,
                    "median_store_bytes": logical,
                    "median_final_canonical_bytes": statistics.median(t["canonical_export_bytes"] for t in trials),
                    "deployment_total_bytes": logical + fixed["deployment_fixed_bytes"],
                    "source_primary_bytes": logical + fixed["source_primary_fixed_bytes"]}
            longest = max(summaries, key=lambda name: (summaries[name]["transactions"], summaries[name]["merges"]))
            by_alias[row["alias"]] = {
                "split": row["split"], "initial_canonical_bytes": row["canonical_bytes"],
                "measurement_sequences": summaries, "rank_sequence": longest, **summaries[longest],
            }
    for case in cases:
        if case.get("scenario") != "measurement" or not case.get("measured_store"):
            continue
        growth = case["measured_store"]["canonical_insert_growth_bytes"]
        selected = [operation for operation in case["operations"] if operation["op"] in ("transaction", "merge", "checkpoint")]
        counter_available = all("write_bytes" in operation.get("io_delta", {}) for operation in selected)
        write_bytes = sum(operation.get("io_delta", {}).get("write_bytes", 0) for operation in selected)
        writes.append({"object_alias": case["object_alias"], "trial": case["metric_trial"],
                       "measurement_sequence": case["measured_store"].get("measurement_sequence", "inserts-2-merges-2"),
                       "canonical_insert_growth_bytes": growth,
                       "kernel_accounted_write_bytes": write_bytes if counter_available else None,
                       "kernel_write_amplification": write_bytes / growth if counter_available and growth else None,
                       "scope": "all transaction and merge request intervals in this sequence; initial import excluded",
                       "counter_semantics": "Linux /proc process-tree write_bytes, not physical device or NAND writes; metadata and page-cache accounting differ from application bytes"})
    peak_rss = max((session.get("observed_peak_rss_bytes", 0) for session in sessions), default=0)
    peak_wait4 = max((session.get("wait4_peak_rss_bytes") or 0 for session in sessions), default=0)
    peak_space = max((session.get("observed_peak_store_bytes", 0) for session in sessions), default=0)
    peak_after = max((operation.get("space_after", {}).get("logical_bytes", 0) for operation in operations), default=0)
    development = [obj for obj in by_alias.values() if obj["split"] == "development"]
    total_store = sum(obj["median_store_bytes"] for obj in development)
    return {
        "operation_latency": metrics.operation_distributions(operations),
        "operation_latency_aggregate": metrics.operation_distributions([
            {**op, "object_alias": "aggregate"} for op in operations if op.get("phase") == "measurement"]),
        "metric_trials": measured, "write_amplification_trials": writes,
        "objects": by_alias,
        "development": {"objects": len(development), "store_bytes": total_store,
            "rank_sequences": sorted({obj["rank_sequence"] for obj in development}),
            "deployment_total_bytes": total_store + fixed["deployment_fixed_bytes"] if development else None,
            "source_primary_bytes": total_store + fixed["source_primary_fixed_bytes"] if development else None,
            "rule": "Sum development stores after the longest declared sequence, plus one shared deployment artifact; no static byte-horizon projection"},
        "observed_peak_rss_bytes": peak_rss, "wait4_peak_rss_bytes": peak_wait4,
        "observed_peak_store_bytes": max(peak_space, peak_after),
        "observed_peak_allocated_bytes": max((session.get("observed_peak_allocated_bytes", 0) for session in sessions), default=0),
        "observer_limitations": "10 ms RSS/space polling and operation-boundary inventories can miss short-lived peaks. wait4 includes namespace/Python bootstrap. These are observed measurements, not hard peak proofs.",
        "evaluator_ack_ledger_bytes": sum(case.get("ledger", {}).get("bytes", 0) for case in cases),
        "evaluator_ledger_accounting": "Separate request/ack evidence, not a candidate runtime dependency. Store-internal token receipts are included in every complete store inventory; embedded receipts are not charged twice.",
        "cache_procedure": {
            "fresh_process": "New namespace and interpreter per trial; input files and system page cache are NOT dropped. hello startup reported separately; build/open request latency starts at request write.",
            "warm_process": "Serial requests in the same persistent process following build/open. Reopening may prepopulate candidate indexes; no claim of cold application data.",
            "repetitions": "Independent store per declared sequence and metric trial; numbered transactions and merges; close/reopen then exact point, join and export.",
            "timing_boundary": "Evaluator perf_counter_ns immediately before first request-byte write through receipt of one complete bounded JSON response. Ledger fsync, reference validation and space scans occur outside operation latency.",
            "os_cold_cache_certified": False, "trial_selection": "All trials retained, including failed or killed trials separately from clean measurement phases. No trimming or fastest-trial selection.",
        },
        "encoding_floor_applied": False, "power_loss_certified": False,
    }


class Evaluation:
    def __init__(self, root, rows, card, out, depth="full", mode="required", cancel=None, deadline=None,
                 *, data_scope="public", train_hashes=None):
        self.root, self.rows, self.card, self.out = Path(root), rows, card, Path(out)
        self.depth, self.mode, self.cancel = depth, mode, cancel
        if data_scope not in ("public", "private"):
            raise Error("invalid_evaluation_data_scope")
        self.data_scope = data_scope
        self.train_hashes = list(train_hashes) if train_hashes is not None else [r["canonical_sha256"] for r in rows if r["split"] == "train"]
        self.out.mkdir(parents=True, exist_ok=True)
        self.started = time.perf_counter()
        self.deadline = deadline or time.monotonic() + card["search_budget"]["wall_seconds"]
        self.raw = {"schema_version": 1, "workload": "mutable_store", "protocol_version": 1,
                    "depth": depth, "status": "running", "quality_passed": False, "eligible": False,
                    "gates": {}, "reason_codes": [], "cases": [], "power_loss_certified": False,
                    "process_crash_validated": False, "certification": "bounded process-crash validation only",
                    "data_scope": data_scope,
                    "unsupported": ["power-loss certification", "updates", "deletes", "concurrent writers",
                                    "cold OS-cache certification"]}

    def save(self):
        save(self.out / "raw.json", self.raw)

    def check(self):
        if self.cancel and Path(self.cancel).exists():
            raise Error("cancelled")
        if time.monotonic() >= self.deadline:
            raise Error("wall_budget_exhausted")

    def gate(self, name, value=True):
        self.check()
        self.raw["gates"][name] = value
        self.save()

    def case(self, row, scenario, *, trial=None, sequence=None, runtime_override=None, diagnostic=False):
        self.check()
        name = f"case-{len(self.raw['cases']):04d}"
        value = StoreCase(self.root, Path(row["source"]).read_bytes(), self.out / "cases" / name,
                          scenario=scenario, limits=self.card["limits"], cancel=self.cancel, deadline=self.deadline,
                          object_alias=row["alias"], metric_trial=trial, runtime_override=runtime_override,
                          diagnostic=diagnostic, measurement_sequence=sequence).run()
        value["evidence_path"] = "cases/" + name
        self.raw["cases"].append(value)
        self.save()
        if not value["quality_passed"]:
            raise Error(value["reason_codes"][0], value.get("error", "Mutable workload case failed"))

    def _diagnostics(self, manifest, registration):
        if manifest["language"] == "python":
            self.gate("language_diagnostics", {"kind": "interpreted_python", "asan_ubsan_applicable": False,
                "scope": "Python source-executed protocol and recovery tests. Shipped interpreter and extension binaries are NOT sanitizer rebuilt."})
            return
        with tempfile.TemporaryDirectory(prefix="clab-mutable-sanitized-") as folder:
            folder = Path(folder)
            nm = registry.native_manifest(manifest)
            build = native.build(self.root / "source", nm, folder / "build", self.mode, True,
                                 {**self.card["limits"], "_deadline": self.deadline}, self.cancel)
            self.raw["sanitizer_build"] = build
            names = [d["soname"] for d in build["dependencies"]["libraries"]]
            if not any(n.startswith("libasan.so") for n in names) or not any(n.startswith("libubsan.so") for n in names):
                raise Error("blocked_toolchain", "Native mutable diagnostics require ASan and UBSan runtime evidence")
            native.make_runtime(self.root / "source", folder / "build", nm, build, folder / "runtime")
            for scenario in ("basic", "merge_reopen_insert", "io_wal_short_write"):
                self.case(self.rows[0], scenario, runtime_override=(folder / "runtime", build["dependencies"]), diagnostic=True)
            self.gate("language_diagnostics", {"kind": "native_asan_ubsan", "bounded_sequences": 3,
                       "dependencies_sanitizer_rebuilt": False, "leak_detection": False})

    def _evidence(self):
        """One flat, hash-locked artifact fits the original job/result transfer."""
        archive = self.out / "mutable-evidence.zip"
        files = []
        for p in sorted((self.out / "cases").rglob("*")) if (self.out / "cases").exists() else []:
            if p.is_symlink():
                raise Error("mutable_evidence_symlink")
            if p.is_file():
                files.append(p)
        # Bound packaging too, not only candidate replies and writable state.
        if sum(p.stat().st_size for p in files) > 256 * 1024 * 1024:
            raise Error("mutable_evidence_size_limit")
        inventory = {p.relative_to(self.out).as_posix(): {"bytes": p.stat().st_size, "sha256": sha(p)} for p in files}
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in files:
                entry = zipfile.ZipInfo(p.relative_to(self.out).as_posix(), (1980, 1, 1, 0, 0, 0))
                entry.external_attr = 0o100644 << 16
                z.writestr(entry, p.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
            z.writestr("EVIDENCE_MANIFEST.json", canonical({"schema_version": 1, "files": inventory,
                        "public_or_synthetic_only": self.data_scope == "public", "data_scope": self.data_scope,
                        "candidate_source_not_included": "Candidate snapshot is separately verified; private evidence stays inside the owner boundary"}))
        self.raw["evidence_files"] = {archive.name: {"sha256": sha(archive), "bytes": archive.stat().st_size}}

    def run(self):
        fixed = {}
        try:
            validate_selection(self.card)
            if self.mode != "required" or runner.probe()["status"] != "available":
                raise Error("mutable_isolation_required")
            m, r = registry.verify(self.root)
            self.raw.update(candidate_digest=r["candidate_digest"], build_cache_key=r["build_cache_key"])
            fixed = registry.costs(m, r)
            self.raw["fixed_costs"] = fixed
            self.raw["relational_objects"] = [{"alias": row["alias"], **profile(Path(row["source"]).read_bytes())} for row in self.rows]
            self.gate("versioned_isolated_persistent_protocol")
            if self.depth == "full":
                with tempfile.TemporaryDirectory(prefix="clab-mutable-rebuild-") as folder:
                    rebuilt = registry.register(Path(folder), self.root / "source", self.mode,
                        {**self.card["limits"], "_deadline": self.deadline},
                        self.train_hashes)
                    if rebuilt["candidate_digest"] != r["candidate_digest"] or rebuilt["build_cache_key"] != r["build_cache_key"]:
                        raise Error("nonreproducible_mutable_build")
                    self.gate("clean_rebuild_exact_runtime", {"candidate_digest": rebuilt["candidate_digest"],
                                                             "build_cache_key": rebuilt["build_cache_key"]})
            for row in self.rows:
                for scenario in SCENARIOS if self.depth == "full" else ("basic",):
                    self.case(row, scenario)
            self.gate("independent_oracle_exact_query_and_export")
            self.gate("atomic_insert_foreign_keys_tokens_and_lexical_spelling")
            if self.depth == "full":
                self.raw["process_crash_validated"] = True
                self.gate("required_recovery_and_followup_writes", {"scenarios": list(SCENARIOS), "objects": len(self.rows),
                    "external_sigkill_cases": sum(bool(case["events"] and any(e["event"] == "external_SIGKILL" for e in case["events"])) for case in self.raw["cases"]),
                    "power_loss_certified": False})
                self._diagnostics(m, r)
                for row in self.rows:
                    for sequence in measurement_sequences(self.card):
                        for trial in range(1, self.card["mutable_policy"]["metric_trials"] + 1):
                            self.case(row, "measurement", trial=trial, sequence=sequence)
                self.gate("repeated_persistent_operation_measurements")
            registry.verify(self.root)
            self.gate("end_digest_exact")
            reasons = []
            if self.depth != "full":
                reasons.append("quick_not_promotable")
            if self.card.get("timing_policy", {}).get("role", "smoke") != "benchmark":
                reasons.append("smoke_not_certified")
            if runner.fingerprint()["effective_cgroup_limits"]["status"] != "verified":
                reasons.append("effective_cgroup_limits_unavailable")
            if not any(row["split"] == "development" for row in self.rows):
                reasons.append("no_development_objects")
            self.raw.update(status="ineligible" if reasons else "eligible", eligible=not reasons,
                            quality_passed=True, reason_codes=reasons)
        except BaseException as exc:
            self.raw.update(status="cancelled" if getattr(exc, "code", None) == "cancelled" else "failed",
                            quality_passed=False, eligible=False,
                            reason_codes=[getattr(exc, "code", "mutable_evaluation_error")], error=str(exc)[:4000],
                            traceback=traceback.format_exc(limit=8))
        if fixed:
            self.raw["mutable_metrics"] = summarize(self.raw["cases"], fixed, self.rows)
        try:
            self._evidence()
        except BaseException as exc:
            self.raw.update(status="failed", quality_passed=False, eligible=False)
            self.raw["reason_codes"].append(getattr(exc, "code", "mutable_evidence_error"))
            self.raw["evidence_error"] = str(exc)[:2000]
        self.raw["wall_seconds"] = time.perf_counter() - self.started
        self.save()
        return self.raw
