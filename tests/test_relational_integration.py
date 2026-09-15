import json
import tempfile
import unittest
from pathlib import Path

from compression_lab import dataset
from compression_lab.engine import Engine
from compression_lab.cli import parser, dispatch
from compression_lab.util import Error, load, save
from compression_lab.workloads.fixtures import create_dataset
from compression_lab.workloads import registry


class RelationalEngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, broken=None):
        card = create_dataset(self.root / 'data', 'mutable_store')
        engine = Engine.init(self.root / 'workspace', card)
        registry.create_reference(engine.root / 'workbench' / 'reference', broken)
        registered = engine.register(engine.root / 'workbench' / 'reference')
        return engine, registered['candidate_digest']

    def test_profiles_real_relational_tables_and_speed_policies(self):
        for name in ('static_relational', 'mutable_store'):
            path = create_dataset(self.root / name, name)
            c = dataset.validate(load(path))
            self.assertEqual(c['objective']['encode_floor_bytes_per_second'], None if name == 'mutable_store' else 100_000_000)
            engine = Engine.init(self.root / (name + '-workspace'), path)
            profile = engine.profile()['metrics']
            self.assertEqual(profile['workload'], name)
            self.assertEqual(profile['relational']['objects'][0]['table_order'], ['titles', 'entities', 'episodes', 'relationships'])
            self.assertGreater(profile['relational']['objects'][0]['tables'][0]['null_cells'], 0)
        invalid = load(self.root / 'mutable_store' / 'dataset-card.json')
        invalid['objective']['encode_floor_bytes_per_second'] = 100_000_000
        with self.assertRaises(Error):
            dataset.validate(invalid)

    def test_cli_evaluation_status_compare_resume_and_evidence_export(self):
        engine, cid = self.create()
        args = parser().parse_args(['evaluate', '--workspace', str(engine.root), '--candidate', cid,
                                    '--workload', 'mutable_store', '--depth', 'quick', '--wait'])
        result1 = dispatch(args)
        self.assertTrue(result1['metrics']['quality_passed'], result1)
        self.assertFalse(result1['metrics']['eligible'])
        self.assertEqual(result1['metrics']['workload'], 'mutable_store')
        self.assertNotIn('encoding_below_100_MBps', result1['reason_codes'])
        result2 = engine.evaluate(cid, depth='quick', wait=True, workload='mutable_store')
        ids = [result1['metrics']['result_id'], result2['metrics']['result_id']]
        before = engine.state()['budgets']
        compared = engine.compare(ids)
        self.assertEqual(len(compared['metrics']['rows']), 2)
        resumed = engine.resume()
        self.assertEqual(resumed['metrics']['next_action']['command'], 'evaluate')
        self.assertEqual(engine.state()['budgets'], before)
        exported = engine.export(ids[0], self.root / 'store-export.zip')
        self.assertEqual(exported['status'], 'exported')
        import zipfile
        with zipfile.ZipFile(self.root / 'store-export.zip') as z:
            self.assertIn('evidence/mutable-evidence.zip', z.namelist())
        evidence = engine.artifact(ids[0], 'mutable-evidence.zip', 0, 256)
        self.assertIsNotNone(evidence['metrics']['data_hex'])

    def test_workload_selection_failure_records_and_terminal_latch(self):
        engine, cid = self.create(broken='wrong_join')
        with self.assertRaises(Error) as caught:
            engine.evaluate(cid, workload='static_relational')
        self.assertEqual(caught.exception.code, 'candidate_workload_mismatch')
        failed = engine.evaluate(cid, depth='quick', wait=True)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(engine.state()['budgets']['agent']['evaluations'], 1)
        self.assertIn('mutable_query_bytes_mismatch', failed['reason_codes'])
        self.assertEqual(engine.resume()['metrics']['next_action']['command'], 'register')
        engine.ledger.transition('failed')
        for operation in (lambda: engine.register(engine.root / 'workbench' / 'reference'),
                          lambda: engine.evaluate(cid)):
            with self.assertRaises(Error) as caught:
                operation()
            self.assertEqual(caught.exception.code, 'terminal_latch')
        self.assertIsNone(engine.resume()['metrics']['next_action'])


if __name__ == '__main__':
    unittest.main()
