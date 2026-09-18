import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from compression_lab import benchmark_run as run
from compression_lab.util import Error


class BenchmarkReceiptTests(unittest.TestCase):
    def test_receipt_pins_bytes_and_separates_incompatible_runs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run, 'host_identity', return_value={
                'machine_id': 'host-a', 'cpu_model': 'test CPU'}):
            root = Path(tmp)
            raw = root / 'input'; raw.write_bytes(b'SELECT 1;\0')
            cpu = min(os.sched_getaffinity(0))
            def receipt(name, **kw):
                return run.write_metadata(root / name, protocol='test-v1', inputs={'queries': raw},
                                          cpu=cpu, **kw)
            first = receipt('a', parameters={'trials': 7})
            same = receipt('b', parameters={'trials': 7})
            self.assertEqual(first['comparison_id'], same['comparison_id'])
            self.assertEqual(first['original_bytes'], 10)
            self.assertEqual(first['inputs'][0]['bytes'], 10)
            changed = receipt('c', parameters={'trials': 3})
            self.assertNotEqual(first['comparison_id'], changed['comparison_id'])
            raw.write_bytes(b'SELECT 2;\0')
            changed_input = receipt('d', parameters={'trials': 7})
            self.assertNotEqual(first['workload_id'], changed_input['workload_id'])
            self.assertEqual(json.loads((root/'a/run-metadata.json').read_text()), first)
            with self.assertRaises(FileExistsError):
                receipt('a')

    def test_lock_excludes_concurrent_measurement_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run.runner, 'HOST_LOCK', Path(tmp)/'lock'):
            with self.assertRaisesRegex(RuntimeError, 'measurement failed'):
                with run.measurement_lock():
                    with self.assertRaises(Error) as caught:
                        with run.measurement_lock():
                            self.fail('Two benchmarks acquired the same lease')
                    self.assertEqual(caught.exception.code, 'benchmark_lease_busy')
                    raise RuntimeError('measurement failed')
            with run.measurement_lock():
                pass


if __name__ == '__main__':
    unittest.main()
