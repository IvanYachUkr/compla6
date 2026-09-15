import tempfile
import unittest
from pathlib import Path

from compression_lab.workloads.fixtures import movie_bundle
from compression_lab.workloads import registry
from compression_lab.workloads.mutable_cases import StoreCase


class MutableCrashRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        registry.create_reference(cls.root / 'good-source')
        r = registry.register(cls.root, cls.root / 'good-source')
        cls.good = cls.root / 'candidates' / r['candidate_digest']

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def scenario(self, candidate, name, folder):
        return StoreCase(candidate, movie_bundle(), self.root / folder, scenario=name).run()

    def test_merge_close_open_ack_insert_close_open(self):
        result = self.scenario(self.good, 'merge_reopen_insert', 'required-sequence-1')
        self.assertTrue(result['quality_passed'], result)
        self.assertGreaterEqual(result['reference_head'], 2)

    def test_killed_merge_recovery_new_transaction_merge_recovery(self):
        for point in ('merge_before_manifest', 'merge_after_manifest'):
            with self.subTest(point=point):
                result = self.scenario(self.good, 'kill_' + point, 'required-sequence-2-' + point)
                self.assertTrue(result['quality_passed'], result)
                self.assertTrue(any(s['returncode'] == -9 for s in result['sessions']))

    def test_short_write_fsync_and_rename_failures(self):
        for fault in ('wal_short_write', 'wal_fsync', 'manifest_rename', 'merge_snapshot_fsync', 'directory_fsync'):
            with self.subTest(fault=fault):
                result = self.scenario(self.good, 'io_' + fault, 'io-' + fault)
                self.assertTrue(result['quality_passed'], result)
                self.assertGreaterEqual(result['reference_head'], 3)
                self.assertFalse(result['power_loss_certified'])

    def test_external_kill_without_hooks_and_uncertain_wal_requests(self):
        for name in ('kill_idle', 'kill_wal_after_fsync', 'kill_wal_short_write'):
            with self.subTest(name=name):
                result = self.scenario(self.good, name, name)
                self.assertTrue(result['quality_passed'], result)
                self.assertTrue(any(s['returncode'] == -9 for s in result['sessions']))

    def test_each_deliberately_broken_candidate_is_detected(self):
        cases = {
            'counter_reset': ('merge_reopen_insert', {'mutable_acknowledged_head_lost_or_unexpected'}),
            'stale_generation_collision': ('kill_merge_before_manifest', {'mutable_candidate_rejected'}),
            'partial_wal_continuation': ('io_wal_short_write', {'mutable_candidate_rejected', 'mutable_acknowledged_head_lost_or_unexpected'}),
            'duplicate_export': ('basic', {'mutable_export_bytes_mismatch'}),
            'partial_transaction': ('basic', {'mutable_rejected_transaction_changed_bytes'}),
            'wrong_join': ('basic', {'mutable_query_bytes_mismatch'}),
        }
        for bug, (scenario, expected) in cases.items():
            with self.subTest(bug=bug):
                path = self.root / ('source-' + bug)
                registry.create_reference(path, broken=bug)
                r = registry.register(self.root, path)
                result = self.scenario(self.root / 'candidates' / r['candidate_digest'], scenario, 'broken-' + bug)
                self.assertFalse(result['quality_passed'], result)
                self.assertIn(result['reason_codes'][0], expected, result)


if __name__ == '__main__':
    unittest.main()
