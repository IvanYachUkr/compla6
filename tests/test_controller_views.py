"""Public diagnostics remain useful when unrelated retained evidence is damaged."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from compression_lab.controller import Controller
from compression_lab.engine import Engine
from compression_lab.util import Error
from test_controller import EvidenceEngine, protocol


class ControllerViews(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = EvidenceEngine(self.temp.name)
        self.engine.card.update(dataset_id="view-fixture", limits={})
        self.engine.card["search_budget"] = self.engine.state()["search_budget"]
        self.engine.card["objective"].update(rank_by="deployment_total_bytes", deployment_canonical_bytes=1000)
        self.engine.add("r-old-control", track="baseline", candidate_digest="control-a")
        self.engine.add("r-current", status="failed", eligible=False, quality_passed=False,
                        reason_codes=["fixture_gate_failure"], error="fixture failure",
                        gates={"roundtrip": {"ok": False}})
        state = {"run_id": "run-test", "card_digest": "card", "runtime_digest": "runtime",
                 "protocol": protocol(), "protocol_digest": "fixture", "lifecycle": "open",
                 "nodes": {}, "completion": None}
        self.controller = object.__new__(Controller)
        self.controller.engine = self.engine
        self.controller.state = lambda: copy.deepcopy(state)
        self.controller.memory_state = lambda: {"mode": "adaptive", "changed": False}
        self.engine._controller = lambda: self.controller
        self.engine.baseline_table = lambda: {"metrics": {}}
        for target, value in (("compression_lab.policy.bridge.verify_candidate", ({"candidate_id": "stored"}, {})),
                              ("compression_lab.research.inventory.inspect", {"families": {}, "recipes": {}})):
            patched = patch(target, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)

    def corrupt(self, result_id, failure):
        original = self.engine.raw
        def read(selected):
            if selected == result_id: raise failure
            return original(selected)
        return patch.object(self.engine, "raw", side_effect=read)

    def test_feedback_preserves_current_diagnostics_and_controller_action(self):
        for failure in (Error("stale_result_digest"), json.JSONDecodeError("truncated", "{", 1)):
            with self.subTest(failure=type(failure).__name__), self.corrupt("r-old-control", failure):
                report = Engine.feedback(self.engine, "r-current")["metrics"]
                self.assertIsNone(report["qualification"])
                self.assertIn("qualification_error", report)
                self.assertEqual(report["reason_codes"], ["fixture_gate_failure"])
                self.assertEqual(report["first_error"], "fixture failure")
                self.assertEqual(report["diagnostic_action"], "fix_first_failing_gate")
                self.assertEqual(report["next_action"], self.controller.brief()["next_action"])
                self.assertEqual(report["next_action"]["command"], "inspect_evidence")

    def test_brief_returns_controller_action_and_explicit_advisory_error(self):
        with self.corrupt("r-old-control", Error("stale_result_digest")):
            report = Engine.brief(self.engine)["metrics"]
            self.assertEqual(report["advisory_error"], "stale_result_digest")
            self.assertEqual(report["next_action"], report["controller"]["next_action"])
            self.assertEqual(report["next_action"]["command"], "inspect_evidence")

    def test_successful_legacy_fields_remain_available(self):
        brief = Engine.brief(self.engine)["metrics"]
        self.assertEqual(brief["dataset_id"], "view-fixture")
        self.assertIn("composition", brief)
        self.assertEqual(brief["promotion_policy"]["encoding_floor_bytes_per_second"], 100)
        self.assertNotIn("advisory_error", brief)
        feedback = Engine.feedback(self.engine, "r-current")["metrics"]
        self.assertFalse(feedback["qualification"]["candidate_qualified"])
        self.assertNotIn("qualification_error", feedback)
        self.assertEqual(feedback["next_action"]["command"], "research")

    def test_corrupt_requested_result_still_fails(self):
        failure = json.JSONDecodeError("truncated", "{", 1)
        with self.corrupt("r-current", failure), self.assertRaises(json.JSONDecodeError):
            Engine.feedback(self.engine, "r-current")

    def test_without_controller_legacy_errors_still_fail(self):
        self.engine._controller = lambda: None
        with self.corrupt("r-old-control", Error("stale_result_digest")):
            with self.assertRaisesRegex(Error, "stale_result_digest"): Engine.brief(self.engine)
            with self.assertRaisesRegex(Error, "stale_result_digest"): Engine.feedback(self.engine, "r-current")


if __name__ == "__main__":
    unittest.main()
