import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import owner, runner
from compression_lab.util import Ledger, digest, load, save


class PrivateRecovery(unittest.TestCase):
    def interrupted(self, root):
        ledger = Ledger(root / 'state')
        ledger.create({'stage': 'private_started', 'run_id': 'private-run',
                       'candidate_digest': 'a' * 64, 'card_digest': 'b' * 64,
                       'runtime_digest': runner.fingerprint()['runtime_digest'],
                       'private_attempts': 1, 'disclosed': False})
        save(root / 'frozen-card.json', {})
        return ledger

    def test_interrupted_attempt_is_consumed_without_reopening_private_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = self.interrupted(root)
            with patch.object(owner, 'boundary', return_value={'policy': {}}), \
                 patch.object(owner.candidate, 'verify'), \
                 patch.object(owner, 'private_rows') as private_read:
                result = owner.evaluate(root, resume=True)
            private_read.assert_not_called()
            self.assertEqual(ledger.read()['private_attempts'], 1)
            self.assertEqual(result['status'], 'failed')
            raw = load(root / 'results' / (result['metrics']['owner_result_id'] + '.json'))
            self.assertEqual(raw['reason_codes'], ['private_attempt_interrupted'])

    def test_completed_outcome_is_recovered_without_new_scoring(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = self.interrupted(root)
            state = ledger.read()
            raw = {'status': 'ineligible', 'quality_passed': True, 'eligible': False,
                   'run_id': state['run_id'], 'candidate_digest': state['candidate_digest'],
                   'card_digest': state['card_digest'], 'runtime_digest': state['runtime_digest'],
                   'private_attempt': 1, 'reason_codes': ['encoding_below_100_MBps']}
            result_id = 'r-' + digest(raw)
            save(root / 'results' / (result_id + '.json'), raw)
            save(root / 'private-attempts/0001/outcome.json', {'stage': 'complete', 'result_id': result_id})
            with patch.object(owner, 'boundary', return_value={'policy': {}}), \
                 patch.object(owner.candidate, 'verify'), \
                 patch.object(owner, 'private_rows') as private_read:
                result = owner.evaluate(root, resume=True)
            private_read.assert_not_called()
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['metrics']['owner_result_id'], result_id)
            self.assertEqual(ledger.read()['private_attempts'], 1)
