import tempfile
import unittest
from pathlib import Path
from compression_lab import candidate, runner


class SandboxPortability(unittest.TestCase):
    def test_bare_command_needs_no_python_files_after_chroot(self):
        binary = Path('/usr/bin/true')
        dependencies = candidate.closure(binary, {'dependencies': []})
        mounts = candidate.runtime_mounts(dependencies)
        mounts['/usr/bin/true'] = str(binary)
        with tempfile.TemporaryDirectory() as td:
            result = runner.execute(['true'], output=Path(td), runtime_files=mounts)
        self.assertEqual(result['returncode'], 0)
