#!/usr/bin/env python3
"""One model-free source-tree acceptance command for the additive workloads.

Usage: python tools/accept_relational.py --output /tmp/clab-relational-acceptance
The output directory must not exist. No package, account or model is installed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from compression_lab import baselines, runner
from compression_lab.engine import Engine
from compression_lab.util import Error, canonical, load, save, sha
from compression_lab.workloads import registry
from compression_lab.workloads.fixtures import create_dataset, movie_bundle
from compression_lab.workloads.mutable_cases import StoreCase

CONTROLS = {
    "counter_reset": ("merge_reopen_insert", {"mutable_acknowledged_head_lost_or_unexpected"}),
    "stale_generation_collision": ("kill_merge_before_manifest", {"mutable_candidate_rejected"}),
    "partial_wal_continuation": ("io_wal_short_write", {"mutable_candidate_rejected", "mutable_acknowledged_head_lost_or_unexpected"}),
    "duplicate_export": ("basic", {"mutable_export_bytes_mismatch"}),
    "partial_transaction": ("basic", {"mutable_rejected_transaction_changed_bytes"}),
    "wrong_join": ("basic", {"mutable_query_bytes_mismatch"}),
}


def audit_export(engine, result_id, dest):
    exported = engine.export(result_id, dest)
    with zipfile.ZipFile(dest) as archive:
        manifest = json.loads(archive.read("EXPORT_MANIFEST.json"))
        import hashlib
        for name, expected in manifest["files"].items():
            data = archive.read(name)
            if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                raise Error("acceptance_export_digest_failed", name)
    save(dest.with_suffix(".manifest.json"), manifest)
    return exported


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    out = Path(args.output).absolute()
    out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    report = {"extension": "0.2.1-local", "status": "running", "model_calls": 0,
              "dataset_policy": "Only generated tiny synthetic relational data, no log/corpus benchmarks",
              "power_loss_certified": False, "mcp_sdk_or_live_host_tested": False}
    save(out / "runtime.json", runner.fingerprint())
    try:
        with (out / "unit-tests.txt").open("wb") as log:
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
            run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
                                  "-p", "test*.py", "-v"], cwd=ROOT, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=1200)
        report["unit_tests"] = {"returncode": run.returncode, "log": "unit-tests.txt", "sha256": sha(out / "unit-tests.txt")}
        if run.returncode:
            raise Error("acceptance_unit_tests_failed")
        engines, results = {}, {}
        for kind in ("static_relational", "mutable_store"):
            parent = out / kind
            parent.mkdir()
            card = create_dataset(parent / "data", kind)
            engine = Engine.init(parent / "workspace", card)
            engines[kind] = engine
            save(parent / "profile.json", engine.profile())
            if kind == "static_relational":
                source = engine.root / "workbench" / "zstd-relational"
                baselines.create(source, "zstd", 1)
                m = load(source / "candidate.json")
                m["input_domain"] = "rlb1-lexical"
                save(source / "candidate.json", m)
                registered = engine.register(source, "baseline")
                result = engine.evaluate(registered["candidate_digest"], depth="full", track="baseline", wait=True, workload=kind)
            else:
                prepared = engine.prepare_baselines(depth="full")
                result_id = prepared["metrics"]["rows"][-1]["result_id"]
                result = engine.result(result_id)
                evaluations_before = engine.state()["budgets"]["baseline"]["evaluations"]
                cached = engine.prepare_baselines(depth="full")
                if engine.state()["budgets"]["baseline"]["evaluations"] != evaluations_before or result_id not in cached["metrics"]["cached_result_ids"]:
                    raise Error("acceptance_exact_baseline_cache_failed")
                save(parent / "baseline-cache.json", cached)
            results[kind] = result
            save(parent / "result.json", result)
            if not result["metrics"]["quality_passed"]:
                raise Error("acceptance_good_candidate_failed", str(result.get("reason_codes")))
            raw = engine.raw(result["metrics"]["result_id"])
            if kind == "mutable_store" and (not raw["process_crash_validated"] or raw["power_loss_certified"]):
                raise Error("acceptance_durability_scope_failed")
            report[kind] = {"result_id": result["metrics"]["result_id"], "candidate_digest": result["candidate_digest"],
                            "quality_passed": True, "eligible": result["metrics"]["eligible"],
                            "reason_codes": result["reason_codes"], "case_count": len(raw.get("cases", [])),
                            "metric_trial_count": result["metrics"].get("metric_trial_count"),
                            "export": audit_export(engine, result["metrics"]["result_id"], parent / "candidate-export.zip")}
            save(parent / "status.json", engine.status())
            save(parent / "resume.json", engine.resume())
            save(out / "ACCEPTANCE.json", report)
        controls = []
        for bug, (scenario, expected) in CONTROLS.items():
            parent = out / "negative-controls" / bug
            parent.mkdir(parents=True)
            registry.create_reference(parent / "source", broken=bug)
            reg = registry.register(parent, parent / "source")
            result = StoreCase(parent / "candidates" / reg["candidate_digest"], movie_bundle(), parent / "case", scenario=scenario).run()
            detected = not result["quality_passed"] and bool(set(result["reason_codes"]) & expected)
            controls.append({"bug": bug, "candidate_digest": reg["candidate_digest"], "scenario": scenario,
                             "detected": detected, "reason_codes": result["reason_codes"]})
            if not detected:
                raise Error("acceptance_negative_control_escaped", bug)
        report["negative_controls"] = controls
        engine = engines["mutable_store"]
        bad_source = engine.root / "workbench" / "broken-join-engine"
        registry.create_reference(bad_source, "wrong_join")
        bad_reg = engine.register(bad_source)
        bad = engine.evaluate(bad_reg["candidate_digest"], depth="full", wait=True)
        if bad["status"] != "failed" or "mutable_query_bytes_mismatch" not in bad["reason_codes"]:
            raise Error("acceptance_engine_failure_not_preserved")
        good_id = results["mutable_store"]["metrics"]["result_id"]
        bad_id = bad["metrics"]["result_id"]
        save(out / "mutable_store" / "engine-negative.json", bad)
        before = engine.state()["budgets"]
        save(out / "mutable_store" / "compare.json", engine.compare([good_id, bad_id]))
        save(out / "mutable_store" / "resume-after-failure.json", engine.resume())
        if before != engine.state()["budgets"]:
            raise Error("acceptance_advisory_changed_budget")
        report["engine_failure_result_id"] = bad_id
        report["status"] = "passed"
        report["scope"] = "Quality/recovery acceptance, not a throughput or power-loss certification"
        # A small readable index. Full distributions and every sample are in raw.json.
        with (out / "summary.csv").open("w", newline="") as f:
            fields = ["workload", "result_id", "quality_passed", "eligible", "canonical_bytes", "framed_archive_bytes",
                      "encoding_MBps", "decoding_MBps", "mutable_cases", "measurement_trials", "process_crash_validated"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for kind, result in results.items():
                raw = engines[kind].raw(result["metrics"]["result_id"])
                writer.writerow({"workload": kind, "result_id": result["metrics"]["result_id"],
                    "quality_passed": raw["quality_passed"], "eligible": raw["eligible"],
                    "canonical_bytes": sum(r["canonical_bytes"] for r in engines[kind].data()[1]),
                    "framed_archive_bytes": raw.get("transport", {}).get("hba_archive_bytes"),
                    "encoding_MBps": raw.get("timing", {}).get("encode", {}).get("median_decimal_MB_per_second"),
                    "decoding_MBps": raw.get("timing", {}).get("decode", {}).get("median_decimal_MB_per_second"),
                    "mutable_cases": len(raw.get("cases", [])), "measurement_trials": len(raw.get("mutable_metrics", {}).get("metric_trials", [])),
                    "process_crash_validated": raw.get("process_crash_validated", False)})
    except BaseException as exc:
        report.update(status="failed", reason_code=getattr(exc, "code", "acceptance_error"),
                      error=str(exc), traceback=traceback.format_exc())
    report["elapsed_seconds"] = time.perf_counter() - started
    save(out / "ACCEPTANCE.json", report)
    print(canonical(report).decode())
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
