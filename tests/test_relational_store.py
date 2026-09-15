import base64
import tempfile
import unittest
from pathlib import Path

from compression_lab.workloads.fixtures import movie_bundle, transaction, row
from compression_lab.workloads import registry
from compression_lab.workloads.session import Session
from compression_lab.workloads.oracle import ReferenceState


def good(response):
    assert response['ok'], response
    return response['result']


class DurableStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = tempfile.TemporaryDirectory()
        cls.root = Path(cls.t.name)
        registry.create_reference(cls.root / 'candidate')
        r = registry.register(cls.root, cls.root / 'candidate')
        cls.candidate = cls.root / 'candidates' / r['candidate_digest']

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.store = Path(self.work.name) / 'store'
        self.oracle = ReferenceState(movie_bundle())

    def tearDown(self):
        self.work.cleanup()

    def build(self, process):
        hello = good(process.request('hello', {}))
        self.assertIn('insert_only', hello['capabilities'])
        meta = good(process.request('build', {'bundle_b64': base64.b64encode(movie_bundle()).decode()}))
        self.oracle.observe(meta)

    def check_bytes(self, process):
        self.assertEqual(base64.b64decode(good(process.request('export', {}))['bytes_b64']), self.oracle.export_bytes())
        for table, key in [('titles', 7), ('entities', 3), ('titles', 2), ('titles', 55555)]:
            self.assertEqual(base64.b64decode(good(process.request('point', {'table': table, 'id': key}))['bytes_b64']), self.oracle.point(table, key))
        for kind in ['title_entities', 'episodes']:
            for key in (7, 2, 9, 999):
                self.assertEqual(base64.b64decode(good(process.request('join', {'kind': kind, 'id': key}))['bytes_b64']), self.oracle.join(kind, key))

    def test_atomic_transactions_replays_fk_and_exact_export(self):
        with Session(self.candidate, self.store) as p:
            self.build(p)
            self.check_bytes(p)
            tx = transaction(1)
            self.oracle.accept(tx, good(p.request('transaction', tx)))
            self.oracle.accept(tx, good(p.request('transaction', tx)))
            conflict = {**transaction(2), 'token': tx['token']}
            self.assertFalse(p.request('transaction', conflict)['ok'])
            bad = {'token': 'reject-bundle', 'records': [row('entities', b'777,new\n'), row('relationships', b'999,987654,777,bad\n')]}
            self.assertFalse(p.request('transaction', bad)['ok'])
            for op in ['update', 'delete', 'concurrent_writer']:
                result = p.request(op, {})
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], 'unsupported_operation')
            self.check_bytes(p)

    def test_merge_close_open_ack_insert_close_open(self):
        with Session(self.candidate, self.store) as p:
            self.build(p)
            self.oracle.accept(transaction(1), good(p.request('transaction', transaction(1))))
            self.oracle.observe(good(p.request('merge', {})))
        with Session(self.candidate, self.store) as p:
            self.oracle.observe(good(p.request('open', {})))
            self.oracle.accept(transaction(2), good(p.request('transaction', transaction(2))))
        with Session(self.candidate, self.store) as p:
            self.oracle.observe(good(p.request('open', {})))
            self.check_bytes(p)
            self.oracle.accept(transaction(1), good(p.request('transaction', transaction(1))))


if __name__ == '__main__':
    unittest.main()
