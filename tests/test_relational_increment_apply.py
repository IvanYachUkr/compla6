"""Increment installer regression tests, independent of any live workspace."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class IncrementApplyTests(unittest.TestCase):
    def fixture(self, root):
        increment, base = root / 'increment', root / 'base'
        (increment / 'overlay').mkdir(parents=True)
        base.mkdir()
        (base / 'engine.py').write_bytes(b'original\n')
        (base / 'original-results.json').write_bytes(b'{"unchanged":true}\n')
        (increment / 'overlay' / 'engine.py').write_bytes(b'extended\n')
        (increment / 'overlay' / 'workload.py').write_bytes(b'new module\n')
        digest = lambda b: hashlib.sha256(b).hexdigest()
        manifest = {'schema_version': 1, 'extension': '0.1.0-relational.1', 'files': {
            'engine.py': {'before_sha256': digest(b'original\n'), 'after_sha256': digest(b'extended\n'), 'after_bytes': 9, 'mode': 420},
            'workload.py': {'before_sha256': None, 'after_sha256': digest(b'new module\n'), 'after_bytes': 11, 'mode': 420}}}
        (increment / 'INCREMENT_MANIFEST.json').write_text(json.dumps(manifest))
        return increment, base

    def invoke(self, increment, base, action):
        return subprocess.run([sys.executable, str(ROOT / 'tools' / 'apply_relational_increment.py'), '--bundle', str(increment), '--base', str(base), action], capture_output=True, text=True)

    def test_apply_preserves_originals_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            increment, base = self.fixture(Path(folder))
            checked = self.invoke(increment, base, '--check')
            self.assertEqual(checked.returncode, 0, checked.stderr + checked.stdout)
            self.assertEqual((base / 'engine.py').read_bytes(), b'original\n')
            applied = self.invoke(increment, base, '--apply')
            self.assertEqual(applied.returncode, 0, applied.stderr + applied.stdout)
            self.assertEqual((base / 'engine.py').read_bytes(), b'extended\n')
            backups = list((base / '.relational-increment-backups').glob('*/engine.py'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b'original\n')
            repeated = self.invoke(increment, base, '--apply')
            self.assertEqual(repeated.returncode, 0, repeated.stderr + repeated.stdout)
            self.assertEqual(json.loads(repeated.stdout)['written_files'], [])
            self.assertEqual((base / 'original-results.json').read_bytes(), b'{"unchanged":true}\n')

    def test_independent_core_fix_conflict_prevents_all_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            increment, base = self.fixture(Path(folder))
            (base / 'engine.py').write_bytes(b'independent user repair\n')
            result = self.invoke(increment, base, '--apply')
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)['status'], 'conflict')
            self.assertEqual(json.loads(result.stdout)['conflicts'][0]['path'], 'engine.py')
            self.assertEqual((base / 'engine.py').read_bytes(), b'independent user repair\n')
            self.assertFalse((base / 'workload.py').exists())
            self.assertFalse((base / '.relational-increment-backups').exists())

    def test_symlinks_and_tampered_overlay_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            increment, base = self.fixture(root)
            (increment / 'overlay' / 'workload.py').write_bytes(b'TAMPERED')
            result = self.invoke(increment, base, '--apply')
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            self.assertIn('tampered overlay', json.loads(result.stdout)['error'])
            self.assertEqual((base / 'engine.py').read_bytes(), b'original\n')
            (increment / 'overlay' / 'workload.py').write_bytes(b'new module\n')
            outside = root / 'external'
            outside.write_bytes(b'original\n')
            (base / 'engine.py').unlink()
            (base / 'engine.py').symlink_to(outside)
            result = self.invoke(increment, base, '--apply')
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            self.assertIn('symlink path', json.loads(result.stdout)['error'])
            self.assertEqual(outside.read_bytes(), b'original\n')


if __name__ == '__main__':
    unittest.main()
