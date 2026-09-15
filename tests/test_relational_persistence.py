"""Independent filesystem perturbations, not candidate-driven success flags."""
import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path

from compression_lab.workloads import registry
from compression_lab.workloads.fixtures import movie_bundle, transaction
from compression_lab.workloads.mutable_cases import StoreCase
from compression_lab.workloads.session import Session


class MutablePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        registry.create_reference(cls.root / 'source')
        reg = registry.register(cls.root, cls.root / 'source')
        cls.candidate = cls.root / 'candidates' / reg['candidate_digest']

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def case(self, suffix=''):
        case = StoreCase(self.candidate, movie_bundle(), self.root / (self._testMethodName + suffix))
        self.addCleanup(case.stop)
        return case

    def test_orphan_generation_and_temp_files_are_not_adopted(self):
        c = self.case()
        c.start(build=True)
        c.transaction(transaction(1))
        c.merge()
        c.stop()
        for name in ('data-00000000000000000002.json', 'wal-00000000000000000002.log', 'CURRENT.tmp', 'leftover.tmp'):
            (c.store / name).write_bytes(b'UNCOMMITTED ORPHAN, not a valid generation')
        c.start()
        self.assertEqual(c.state.generation, 1)
        c.verify_all()
        c.transaction(transaction(2))
        c.merge()
        c.stop()
        c.start()
        c.verify_all()
        receipt = c.transaction(transaction(1))
        self.assertTrue(receipt['replayed'])
        self.assertEqual(receipt['txid'], 1)
        self.assertEqual(c.state.generation, 2)
        self.assertFalse((c.store / 'leftover.tmp').exists())
        self.assertFalse((c.store / 'CURRENT.tmp').exists())

    def test_incomplete_tail_is_truncated_before_further_acknowledgments(self):
        for tail in (b'\x20', b'\x20\x00\x00', struct.pack('<I', 1024) + b'partial-payload'):
            with self.subTest(tail=tail):
                c = self.case('-' + str(len(tail)))
                c.start(build=True)
                c.transaction(transaction(1))
                c.stop()
                current = json.loads((c.store / 'CURRENT').read_bytes())
                wal = c.store / current['wal']
                size = wal.stat().st_size
                with wal.open('ab') as f:
                    f.write(tail)
                c.start()
                self.assertEqual(wal.stat().st_size, size)
                c.verify_all()
                c.transaction(transaction(2))
                c.merge()
                c.stop()
                c.start()
                c.verify_all()
                self.assertEqual(c.state.head, 2)
                c.stop()

    def test_corrupt_complete_wal_or_snapshot_refuses_and_never_resets_empty(self):
        for fault, code in [('wal', 'wal_checksum'), ('snapshot', 'snapshot_checksum'),
                            ('generation', 'stale_wal_generation'), ('current', 'invalid_data')]:
            with self.subTest(fault=fault):
                c = self.case('-' + fault)
                c.start(build=True)
                c.transaction(transaction(1))
                c.stop()
                current = json.loads((c.store / 'CURRENT').read_bytes())
                if fault in ('wal', 'generation'):
                    path = c.store / current['wal']
                    raw = bytearray(path.read_bytes())
                    raw[-1 if fault == 'wal' else 4] ^= 1
                    path.write_bytes(raw)
                elif fault == 'snapshot':
                    path = c.store / current['snapshot']
                    path.write_bytes(path.read_bytes() + b'X')
                else:
                    (c.store / 'CURRENT').write_bytes(b'{broken json')
                before = {p.name: p.read_bytes() for p in c.store.iterdir() if p.is_file()}
                with Session(self.candidate, c.store) as p:
                    result = p.request('open', {})
                    self.assertFalse(result['ok'], result)
                    self.assertEqual(result['error']['code'], code)
                    self.assertFalse(p.request('transaction', transaction(2))['ok'])
                    self.assertFalse(p.request('export', {})['ok'])
                self.assertEqual(before, {p.name: p.read_bytes() for p in c.store.iterdir() if p.is_file()})

    def test_actual_second_writer_is_rejected_without_damaging_first(self):
        c = self.case()
        c.start(build=True)
        with Session(self.candidate, c.store) as other:
            result = other.request('open', {})
            self.assertFalse(result['ok'])
            self.assertEqual(result['error']['code'], 'single_writer_busy')
            c.transaction(transaction(1))
            c.verify_all()
        c.stop()
        c.start()
        c.verify_all()

    def test_refused_transaction_has_no_wal_or_export_side_effects(self):
        c = self.case()
        c.start(build=True)
        before = {p.name: p.read_bytes() for p in c.store.iterdir() if p.is_file()}
        valid = transaction(1)
        # Stage valid title/entity first, then a missing foreign-key row.
        invalid = {**valid, 'records': valid['records'][:2] + [
            {'table': 'relationships', 'record_b64': base64.b64encode(b'900,999999,1001,broken\n').decode()}]}
        c.reject('transaction', invalid)
        self.assertEqual(before, {p.name: p.read_bytes() for p in c.store.iterdir() if p.is_file()})
        c.transaction(valid)
        c.stop()
        c.start()
        c.verify_all()


if __name__ == '__main__':
    unittest.main()
