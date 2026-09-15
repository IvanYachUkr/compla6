"""A fast replay cannot qualify while its required preparation misses the floor."""
import tempfile
import unittest
from unittest.mock import patch

from compression_lab import research
from compression_lab.controller import Controller
from compression_lab.policy import PublicPolicy
from test_controller import EvidenceEngine, protocol


class EndToEndFloorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.engine = EvidenceEngine(temp.name)
        self.engine.card['objective']['encode_floor_bytes_per_second'] = 100_000_000
        self.engine.card['timing_policy'].update(operation='offline-plus-online-v1', encoding_floor_scope='combined')
        self.engine._controller = lambda: None
        verifier = patch('compression_lab.policy.bridge.verify_candidate', return_value=({}, {}))
        verifier.start(); self.addCleanup(verifier.stop)

    def add(self, combined):
        timing = {'encode':{'median_bytes_per_second':614_000_000}, 'offline':{'median_seconds':10.94}}
        if combined is not None: timing['combined'] = {'median_bytes_per_second':combined}
        return self.engine.add(timing_operation='offline-plus-online-v1', encoding_floor_scope='combined', timing=timing)

    def test_slow_or_missing_total_cannot_inherit_a_fast_online_claim(self):
        for combined in (9_000_000, None):
            with self.subTest(combined=combined):
                rid = self.add(combined)
                assessment = PublicPolicy(self.engine).assess(rid)
                self.assertFalse(assessment['candidate_qualified'])
                self.assertIn('encode_floor_not_met', assessment['reason_codes'])

    def test_fast_complete_pipeline_qualifies_and_the_floor_is_explicit(self):
        rid = self.add(101_000_000)
        assessment = PublicPolicy(self.engine).assess(rid)
        self.assertTrue(assessment['candidate_qualified'])
        self.assertEqual(assessment['encoding_floor_scope'], 'combined')
        controller = Controller.configure(self.engine, protocol())
        self.assertEqual(controller.brief()['evidence_contract']['encoding_floor_scope'], 'combined')

    def test_feedback_uses_total_headroom_and_retains_the_correct_dense_result(self):
        rid = self.add(9_000_000)
        self.engine.rows[rid].update(status='ineligible', eligible=False, reason_codes=['encoding_below_100_MBps'])
        result = research.feedback(self.engine, rid)
        self.assertEqual(result['next_action'], 'optimize_measured_bottleneck')
        self.assertEqual(result['encode_floor_headroom_MBps'], -91)
        self.assertEqual(result['encoding_floor_scope'], 'combined')
        self.assertTrue(result['quality_passed'])
        self.assertFalse(result['qualification']['candidate_qualified'])
        self.assertEqual(result['encoding_stages']['online']['median_bytes_per_second'], 614_000_000)


if __name__ == '__main__': unittest.main()
