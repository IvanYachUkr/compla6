"""Curated model-free policy replays are reproducible and preserve their outputs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from controller_impact import load_before, run_report


class ControllerImpactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = ROOT / 'results' / 'controller-impact'
        parent.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='test-', dir=parent)
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.output = Path(cls.tmp.name) / 'report'
        cls.report = run_report(cls.output)
        cls.cases = {case['id']: case for case in cls.report['cases']}

    def test_final_trial_success_is_retained(self):
        case = self.cases['final_trial_success']
        self.assertEqual(case['before']['public_decision']['action'], 'budget_exhausted')
        self.assertEqual(case['after']['brief']['next_action']['outcome'], 'success')
        self.assertTrue(case['after']['finish']['accepted'])
        self.assertTrue(case['after']['finish']['candidate_qualified'])

    def test_baseline_remains_a_control(self):
        case = self.cases['baseline_is_not_submission']
        self.assertEqual(case['before']['resume']['metrics']['next_action']['command'], 'export')
        self.assertEqual(case['before']['public_decision']['action'], 'retry')
        self.assertEqual(case['after']['brief']['next_action']['command'], 'research')
        self.assertFalse(case['after']['finish']['accepted'])
        self.assertIn('track_mismatch', case['after']['finish']['reason_codes'])

    def test_full_ineligible_result_continues_research(self):
        case = self.cases['full_ineligible_continues']
        before = case['before']['resume']['metrics']['next_action']
        self.assertEqual(before['command'], 'export')
        self.assertFalse(before['eligible'])
        self.assertEqual(case['after']['brief']['next_action']['command'], 'research')
        self.assertFalse(case['after']['finish']['accepted'])

    def test_idle_parent_waits_for_active_child(self):
        case = self.cases['idle_parent_active_child']
        self.assertEqual(case['before']['resume']['metrics']['next_action']['command'], 'export')
        self.assertEqual(case['after']['brief']['lifecycle'], 'waiting')
        self.assertEqual(case['after']['brief']['outstanding']['children'], ['worker'])
        self.assertEqual(case['after']['brief']['next_action']['command'], 'wait')
        self.assertIn('outstanding_work', case['after']['finish']['reason_codes'])

    def test_dispatch_is_not_acknowledgment_and_reopen_does_not_resend(self):
        case = self.cases['feedback_dispatch_acknowledgment']
        self.assertEqual(case['before']['saved_after_queue']['delivered'], ['synthetic-feedback'])
        self.assertIsNone(case['before']['observed_native_receipt'])
        self.assertEqual(case['after']['after_reopen']['pending'], [])
        self.assertEqual(case['after']['after_reopen']['unacknowledged'][0]['status'], 'dispatched')
        self.assertEqual(case['after']['after_ack_reopen']['status'], 'acknowledged')
        self.assertEqual(case['after']['after_ack_reopen']['native_receipt'], 'synthetic-native-receipt')
        self.assertEqual(case['after']['after_ack_reopen']['attempts'], 1)

    def test_raw_report_is_explicit_about_evidence_and_contains_assertions(self):
        report = json.loads((self.output / 'report.json').read_text())
        self.assertEqual(report, self.report)
        self.assertEqual(report['evidence_kind'], 'curated_model_free_regression_replay')
        self.assertEqual(report['model_calls'], 0)
        self.assertFalse(report['measured_throughput'])
        self.assertFalse(report['task_success_rate_estimated'])
        self.assertEqual(len(report['cases']), 5)
        self.assertTrue(all(row['passed'] for case in report['cases'] for row in case['assertions']))
        self.assertTrue(all(case['fixture']['synthetic'] for case in report['cases']))
        self.assertIn('public_decision', report['provenance']['before']['sources'])
        self.assertIn('compression_lab/controller.py', report['provenance']['after'])

    def test_existing_report_is_never_overwritten(self):
        before = (self.output / 'report.json').read_bytes()
        with self.assertRaises(FileExistsError):
            run_report(self.output)
        self.assertEqual((self.output / 'report.json').read_bytes(), before)

    def test_modified_preserved_function_is_rejected(self):
        source = ROOT / 'provenance' / 'controller-before-policy.py'
        altered = Path(self.tmp.name) / 'altered.py'
        altered.write_text(source.read_text().replace("{'action': 'budget_exhausted'}", "{'action': 'qualified'}", 1))
        with self.assertRaisesRegex(ValueError, 'preserved source hash mismatch'):
            load_before(altered)


if __name__ == '__main__':
    unittest.main()
