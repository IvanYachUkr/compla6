import tempfile
import unittest
from pathlib import Path

from compression_lab.util import Error
from compression_lab.workloads import metrics, registry
from compression_lab.workloads.mutable import summarize


class MutableMetricsTests(unittest.TestCase):
    def test_every_runtime_byte_and_launch_manifest_is_charged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry.create_reference(root / 'source')
            r = registry.register(root, root / 'source')
            m, r = registry.verify(root / 'candidates' / r['candidate_digest'])
            c = registry.costs(m, r)
            self.assertEqual(c['deployment_fixed_bytes'], c['runtime_inventory_bytes'] + c['config_bytes'])
            self.assertGreater(c['config_bytes'], 0)
            self.assertEqual(c['source_primary_fixed_bytes'], c['fixed_bytes'] + c['packed_source_bytes'] + c['language_runtime_bytes'] + c['nonplatform_dependency_bytes'])

    def test_no_trimmed_samples_or_cross_object_merge_round_pooling(self):
        operations = [dict(phase='measurement', cache='warm_process', op='merge', ok=True,
                           object_alias='movie-' + str(i // 2), merge_round=(i % 2) + 1, latency_ns=i + 1)
                      for i in range(4)]
        results = metrics.operation_distributions(operations)
        self.assertEqual(len(results), 4)
        d = metrics.distribution([1, 2, 3, 4, 1000])
        self.assertEqual(d['samples_ns'], [1, 2, 3, 4, 1000])
        self.assertEqual(d['p99_ns'], 1000)
        self.assertEqual(d['median_ns'], 3)

    def test_complete_space_categories_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, data in [('wal-1.log', b'w' * 3), ('data-1.json', b's' * 4), ('CURRENT', b'm'),
                               ('receipts.json', b'a' * 2), ('index.idx', b'i'), ('CURRENT.tmp', b't' * 5), ('extra', b'x')]:
                (root / name).write_bytes(data)
            got = metrics.space(root, hashes=True)
            self.assertEqual(got['logical_bytes'], 17)
            self.assertEqual(sum(got['categories'].values()), 17)
            self.assertEqual(got['categories']['wal'], 3)
            self.assertTrue(all('sha256' in x for x in got['files']))
            (root / 'bad').symlink_to(root / 'CURRENT')
            with self.assertRaises(Error):
                metrics.space(root)

    def test_write_amplification_uses_actual_counter_and_growth_not_stdout(self):
        case = {'scenario': 'measurement', 'object_alias': 'a', 'metric_trial': 1, 'operations': [
                    {'op': 'transaction', 'latency_ns': 1, 'io_delta': {'write_bytes': 4096, 'wchar': 999999}},
                    {'op': 'merge', 'latency_ns': 2, 'io_delta': {'write_bytes': 8192, 'wchar': 999999}}],
                'measured_store': {'canonical_insert_growth_bytes': 512, 'object_alias': 'a',
                                  'canonical_export_bytes': 1024, 'store': {'logical_bytes': 2000}}}
        fixed = {'deployment_fixed_bytes': 100, 'source_primary_fixed_bytes': 300}
        result = summarize([case], fixed, [{'alias': 'a', 'split': 'development', 'canonical_bytes': 512}])
        self.assertEqual(result['write_amplification_trials'][0]['kernel_write_amplification'], 24)
        self.assertEqual(result['objects']['a']['deployment_total_bytes'], 2100)
        self.assertFalse(result['encoding_floor_applied'])
        del case['operations'][1]['io_delta']['write_bytes']
        result = summarize([case], fixed, [])
        self.assertIsNone(result['write_amplification_trials'][0]['kernel_write_amplification'])


if __name__ == '__main__':
    unittest.main()
