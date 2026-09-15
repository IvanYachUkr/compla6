"""Recover only the exact inactive failed registration; preserve native evidence."""
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT/'tools/prime_recover.py'
recovery = types.SimpleNamespace()
if PATH.exists():
    spec = importlib.util.spec_from_file_location('prime_recover', PATH)
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.profile = self.base/'profile'
        self.registry = self.profile/'.prime/agent/daemon-workers/host'
        self.registry.mkdir(parents=True)
        self.original = self.registry/'exact-worker.json'
        self.descriptor = {'version': 1, 'rootSessionId': 'same-root',
                           'sessionFile': '/native/same.jsonl', 'lifecycle': 'failed',
                           'ownerClientId': 'old-client', 'pid': 32,
                           'authenticationToken': 'private-fixture-secret'}
        self.original.write_text(json.dumps(self.descriptor))
        self.parent = {'session_id': 'same-root', 'session_file': '/native/same.jsonl'}
        self.campaign = {'profile': str(self.profile), 'native_session': self.parent,
                         'driver_unit': 'fixture.service', 'agent_uid': 12345}
        self.output = self.base/'recovery'

    def recover(self, unit_state=None, pgrep_code=1):
        self.assertTrue(callable(getattr(recovery, 'recover', None)),
                        'The package does not yet provide guarded failed-registration recovery')
        state = unit_state or {'ActiveState': 'inactive', 'MainPID': '0', 'ControlGroup': ''}
        with patch.object(recovery, 'unit_state', return_value=state), patch.object(
                recovery.subprocess, 'run', return_value=types.SimpleNamespace(returncode=pgrep_code)):
            return recovery.recover(self.campaign, self.output, apply=True)

    def test_exact_failed_descriptor_is_quarantined_and_native_files_preserved(self):
        sessions = self.profile/'.prime/agent/sessions'; sessions.mkdir()
        retained = sessions/'same.jsonl'; retained.write_text('native-session-evidence\n')
        lease = sessions/'same.lease'; lease.write_text('retained-lease\n')
        unrelated = self.registry/'other.json'
        unrelated.write_text(json.dumps({**self.descriptor, 'rootSessionId': 'other-root'}))
        receipt = self.recover()
        self.assertFalse(self.original.exists())
        quarantined = Path(receipt['quarantined_to'])
        self.assertEqual(json.loads(quarantined.read_text()), self.descriptor)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual(quarantined.stat().st_mode & 0o777, 0o600)
        self.assertEqual(retained.read_text(), 'native-session-evidence\n')
        self.assertEqual(lease.read_text(), 'retained-lease\n')
        self.assertTrue(unrelated.exists())
        self.assertNotIn('private-fixture-secret', json.dumps(receipt))

    def test_any_live_native_process_blocks_quarantine(self):
        with self.assertRaisesRegex(RuntimeError, 'process'):
            self.recover(pgrep_code=0)
        self.assertTrue(self.original.exists())
        self.assertFalse(self.output.exists())

    def test_active_driver_blocks_quarantine(self):
        with self.assertRaisesRegex(RuntimeError, 'inactive'):
            self.recover(unit_state={'ActiveState': 'active', 'MainPID': '44', 'ControlGroup': ''})
        self.assertTrue(self.original.exists())

    def test_multiple_matches_or_nonfailed_descriptor_are_refused(self):
        (self.registry/'duplicate.json').write_text(json.dumps(self.descriptor))
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            self.recover()
        (self.registry/'duplicate.json').unlink()
        self.original.write_text(json.dumps({**self.descriptor, 'lifecycle': 'ready'}))
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            self.recover()
        self.assertTrue(self.original.exists())


if __name__ == '__main__':
    unittest.main()
