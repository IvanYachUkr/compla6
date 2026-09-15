import tempfile
import unittest
from pathlib import Path

from compression_lab import runner


class CgroupFingerprint(unittest.TestCase):
    def test_limits_include_tighter_ancestors_of_actual_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hierarchy = root / 'cgroup'
            leaf = hierarchy / 'users/session'
            leaf.mkdir(parents=True)
            membership = root / 'membership'
            membership.write_text('0::/users/session\n')
            for path, cpu, memory in [(hierarchy, 'max 100000', 'max'),
                                       (leaf.parent, '50000 100000', '536870912'),
                                       (leaf, '150000 100000', '1073741824')]:
                (path / 'cpu.max').write_text(cpu)
                (path / 'memory.max').write_text(memory)
            (leaf / 'cpuset.cpus.effective').write_text('2-5')
            actual = runner.cgroup_limits(hierarchy, membership)
            self.assertEqual(actual['status'], 'verified')
            self.assertEqual(actual['cpu_quota_cores'], .5)
            self.assertEqual(actual['memory_max_bytes'], 536870912)
            self.assertEqual(actual['cpuset_cpus'], '2-5')

    def test_missing_process_scope_is_reported_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            membership = root / 'membership'
            membership.write_text('0::/missing\n')
            self.assertEqual(runner.cgroup_limits(root, membership)['status'], 'unverified')
