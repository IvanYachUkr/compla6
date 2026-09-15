"""Profile identity and decoder-accounting selection for the 0.4 engine."""
from __future__ import annotations

import tempfile
import time
import unittest
import os
from unittest.mock import patch
from pathlib import Path

from compression_lab import baselines, dataset, runner
from compression_lab.util import Error, digest, load, save
from compression_lab.workloads import bridge
from test_engine import data


class MorningEngineProfiles(unittest.TestCase):
    def _profiled_card(self, root):
        card_path = data(root / "data")
        card = load(card_path)
        available = sorted(runner.resolve_resources(2)["cpus"])
        card["limits"].update(threads=2, cpus=available)
        card["resource_profiles"] = {
            "schema_version": 1,
            "primary": "primary-two",
            "profiles": [
                {"id": "primary-two", "threads": 2, "cpus": available},
                {"id": "reference-one", "threads": 1, "cpus": [available[0]]},
            ],
        }
        card["timing_policy"]["scope"] = "validation-only-v2"
        card["accounting_policy"] = "supervisor-decoder-v1"
        save(card_path, card)
        return card_path

    def test_init_pins_primary_and_reference_resource_fingerprints(self):
        from compression_lab.engine import Engine

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine.init(root / "workspace", self._profiled_card(root), exploratory=True)
            primary = load(engine.root / "runtime.json")
            profiles = load(engine.root / "runtime-profiles.json")
            self.assertEqual(primary["native_resources"]["threads"], 2)
            self.assertEqual(primary["native_resources"]["cpus"], profiles["profiles"]["primary-two"]["resource_profile"]["cpus"])
            self.assertEqual(profiles["primary"], "primary-two")
            self.assertEqual(profiles["profiles"]["reference-one"]["resource_profile"]["threads"], 1)
            self.assertNotEqual(profiles["profiles"]["primary-two"]["runtime"]["runtime_digest"],
                                profiles["profiles"]["reference-one"]["runtime"]["runtime_digest"])
            table = engine.baseline_table()["metrics"]
            self.assertEqual(table["resource_profile"]["id"], "primary-two")
            self.assertTrue(set(baselines.declared_morning_recipes()).issubset(
                {row["recipe_id"] for row in table["rows"] if row.get("state") == "missing"}))
            self.assertEqual(runner.require_fingerprint(profiles["profiles"]["primary-two"]["runtime"])["runtime_digest"],
                             profiles["profiles"]["primary-two"]["runtime"]["runtime_digest"])
            self.assertEqual(runner.require_fingerprint(profiles["profiles"]["reference-one"]["runtime"])["runtime_digest"],
                             profiles["profiles"]["reference-one"]["runtime"]["runtime_digest"])

    def test_tampered_secondary_profile_fingerprint_is_rejected(self):
        from compression_lab.engine import Engine

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine.init(root / "workspace", self._profiled_card(root), exploratory=True)
            profiles = load(engine.root / "runtime-profiles.json")
            profiles["profiles"]["reference-one"]["runtime"]["python"] = "tampered"
            save(engine.root / "runtime-profiles.json", profiles)
            with self.assertRaises(Error) as caught:
                engine._profile_context("reference-one")
            self.assertEqual(caught.exception.code, "stale_resource_profile_fingerprint")

            # The workspace digest protects the normal case.  This second check
            # verifies the runtime fingerprint itself rather than relying on the
            # outer profile map alone.
            engine.ledger.update("test_profile_tamper", lambda state: {
                **state, "runtime_profiles_digest": digest(profiles["profiles"])
            })
            with self.assertRaises(Error) as caught:
                engine._profile_context("reference-one")
            self.assertEqual(caught.exception.code, "invalid_runtime_fingerprint")

    def test_supervisor_decoder_policy_ranks_exact_development_total(self):
        raw = {
            "accounting_policy": "supervisor-decoder-v1",
            "decoder_accounting": {"development": {"actual": {"deployment_total_bytes": 47}}},
            "accounting": {"development": {"projection": {"deployment_total_bytes": 10_000}}},
        }
        self.assertEqual(bridge.rank_cost(raw), 47)

    def test_baseline_table_keeps_declared_missing_controls_visible(self):
        from compression_lab.engine import Engine
        from compression_lab import baselines

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine.init(root / "workspace", data(root / "data"), exploratory=True)
            rows = engine.baseline_table()["metrics"]["rows"]
            missing = {row["recipe_id"]: row for row in rows if row.get("state") == "missing"}
            self.assertTrue(set(baselines.recipes()).issubset(missing))
            self.assertTrue(all("size_rank" not in row for row in missing.values()))

    def test_baseline_table_does_not_duplicate_a_measured_control_placeholder(self):
        from compression_lab.engine import Engine

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine.init(root / "workspace", data(root / "data"), exploratory=True)
            state = engine.state()
            _, pin = engine._profile_context()
            raw = {
                "track": "baseline", "candidate_digest": "candidate-for-stored",
                "card_digest": state["card_digest"], "runtime_digest": state["runtime_digest"],
                "workload": "independent_objects", "depth": "screen",
                "resource_profile": "legacy-single",
                "resource_profile_digest": pin["resource_profile_digest"],
                "resource_runtime_digest": pin["runtime"]["runtime_digest"],
                "timing_scope": "train-plus-development-v1", "accounting_policy": "standalone-v1",
                "status": "ineligible", "quality_passed": False, "eligible": False,
                "timing_trials": {}, "timing": {},
            }
            engine.ledger.update("test_baseline_result", lambda current: {
                **current, "results": ["r-stored-screen"]
            })
            with patch.object(engine, "raw", return_value=raw), patch(
                    "compression_lab.engine.bridge.verify_candidate", return_value=({"candidate_id": "stored"}, {})):
                stored = [row for row in engine.baseline_table()["metrics"]["rows"]
                          if row.get("recipe_id") == "stored"]
                selected = [row for row in engine.baseline_table(candidate_result="r-stored-screen")["metrics"]["rows"]
                            if row.get("recipe_id") == "stored"]
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["result_id"], "r-stored-screen")
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["kind"], "candidate")

    def test_worker_gets_only_explicit_evaluator_toolchain_environment(self):
        from compression_lab.engine import Engine

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine.init(root / "workspace", data(root / "data"), exploratory=True)
            _, pin = engine._profile_context()
            engine.ledger.update("test_registered_candidate", lambda state: {
                **state, "registered": ["candidate-env"]
            })
            manifest = {"candidate_id": "env-fixture", "input_domain": "opaque_bytes"}
            with patch.dict(os.environ, {
                    "COMPRESSION_LAB_RUST_TOOLCHAIN": "/evaluator/rust",
                    "COMPRESSION_LAB_NATIVE_PREFIX": "/evaluator/native",
                    "PYTHONPATH": "/untrusted/python",
                    "UNRELATED": "not-forwarded",
            }, clear=True), patch(
                    "compression_lab.engine.bridge.verify_candidate", return_value=(manifest, {})), patch(
                    "compression_lab.engine.runner.require_fingerprint", return_value=pin["runtime"]), patch(
                    "compression_lab.engine.subprocess.Popen") as popen:
                engine.evaluate("candidate-env")
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["COMPRESSION_LAB_RUST_TOOLCHAIN"], "/evaluator/rust")
            self.assertEqual(env["COMPRESSION_LAB_NATIVE_PREFIX"], "/evaluator/native")
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("UNRELATED", env)

    def test_partial_new_profile_job_identity_is_not_treated_as_legacy(self):
        from compression_lab.engine import Engine

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine.init(root / "workspace", self._profiled_card(root), exploratory=True)
            job = {"job_id": "partial-profile", "run_id": engine.state()["run_id"],
                   "candidate_digest": "partial-candidate", "track": "agent", "depth": "full",
                   "status": "running", "created_epoch": time.time(),
                   "resource_profile": "primary-two"}
            engine.ledger.update("test_partial_profile_job", lambda state: {
                **state, "active_job": job["job_id"],
                "budgets": {**state["budgets"], "agent": {"evaluations": 1, "wall_seconds": 0}},
            })
            save(engine.root / "jobs" / job["job_id"] / "job.json", job)
            result_id = engine._finish_job(dict(job), {
                "status": "failed", "quality_passed": False, "eligible": False,
                "reason_codes": ["fixture_only"], "wall_seconds": 0,
            })
            with self.assertRaises(Error) as caught:
                engine.raw(result_id)
            self.assertEqual(caught.exception.code, "result_job_mismatch")


if __name__ == "__main__":
    unittest.main()
