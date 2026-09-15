import base64
import tempfile
import unittest
from pathlib import Path

from compression_lab.util import Error
from compression_lab.workloads.fixtures import movie_bundle, transaction, row
from compression_lab.workloads.oracle import ReferenceState, RequestLedger


class RelationalOracleTests(unittest.TestCase):
    def test_initial_export_is_source_and_queries_keep_complete_rows(self):
        state = ReferenceState(movie_bundle())
        self.assertEqual(state.export_bytes(), movie_bundle())
        point = state.point('titles', 7)
        self.assertIn(b'007,tt0000007', point)
        self.assertIn(b'\r\nTitle', point)
        self.assertIn(b'An ""entity""', state.join('title_entities', 7))
        self.assertIn(b'2,7,-0,\\N\r\n', state.join('episodes', 7))
        self.assertEqual(state.point('titles', 987), b'QRY1\0\0\0\0')

    def test_atomic_stage_and_token_receipts(self):
        state = ReferenceState(movie_bundle())
        tx = transaction(1)
        before = state.export_bytes()
        state.prepare(tx)
        self.assertEqual(state.export_bytes(), before)
        state.accept(tx, {'txid': 1, 'head': 1, 'generation': 0, 'checkpoint': 0, 'replayed': False})
        once = state.export_bytes()
        state.accept(tx, {'txid': 1, 'head': 1, 'generation': 0, 'checkpoint': 0, 'replayed': True})
        self.assertEqual(state.export_bytes(), once)
        conflict = {**transaction(2), 'token': tx['token']}
        with self.assertRaises(Error):
            state.prepare(conflict)
        bad = {'token': 'atomic-invalid', 'records': [row('entities', b'777,new\n'), row('relationships', b'888,99999,777,broken\n')]}
        with self.assertRaises(Error):
            state.prepare(bad)
        self.assertEqual(state.export_bytes(), once)

    def test_metadata_never_moves_backwards(self):
        state = ReferenceState(movie_bundle())
        state.accept(transaction(1), {'txid': 1, 'head': 1, 'generation': 0, 'checkpoint': 0, 'replayed': False})
        state.observe({'head': 1, 'generation': 1, 'checkpoint': 1})
        for response in ({'head': 0, 'generation': 1, 'checkpoint': 1},
                         {'head': 1, 'generation': 0, 'checkpoint': 1},
                         {'head': 1, 'generation': 1, 'checkpoint': 0}):
            with self.subTest(response=response), self.assertRaises(Error):
                state.observe(response)

    def test_request_ack_ledger_tamper_and_pending(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'ledger.jsonl'
            ledger = RequestLedger(p)
            a = ledger.request('session-1', 'transaction', transaction(1))
            self.assertEqual(len(ledger.pending()), 1)
            ledger.response(a, {'version': 1, 'id': 2, 'ok': True, 'result': {'txid': 1}})
            self.assertEqual(ledger.pending(), [])
            self.assertEqual(len(ledger.acknowledged()), 1)
            p.write_bytes(p.read_bytes().replace(b'session-1', b'session-2', 1))
            with self.assertRaises(Error):
                ledger.verify()


if __name__ == '__main__':
    unittest.main()
