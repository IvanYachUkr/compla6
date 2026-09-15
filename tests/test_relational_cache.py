import tempfile
import unittest
from pathlib import Path

from compression_lab import dataset
from compression_lab.engine import Engine
from compression_lab.util import Error, load, save, sha
from compression_lab.workloads import registry, relational
from compression_lab.workloads.fixtures import create_dataset


class RelationalCacheAndBoundsTests(unittest.TestCase):
    def test_exact_baseline_cache_key_and_failed_result_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = Engine.init(root / 'work', create_dataset(root / 'data', 'mutable_store'))
            first = engine.prepare_baselines(depth='quick')
            rid = first['metrics']['rows'][-1]['result_id']
            cached = engine.prepare_baselines(depth='quick')
            self.assertIn(rid, cached['metrics']['cached_result_ids'])
            self.assertEqual(engine.state()['budgets']['baseline']['evaluations'], 1)
            source = next((engine.root / 'workbench').glob('mutable-reference-*')) / 'program.py'
            old, new = registry.BROKEN_CONTROLS['wrong_join']
            source.write_text(source.read_text().replace(old, new))
            failed = engine.prepare_baselines(depth='quick')
            failed_row = failed['metrics']['rows'][-1]
            self.assertEqual(failed_row['status'], 'failed')
            self.assertNotEqual(rid, failed_row['result_id'])
            self.assertNotEqual(first['metrics']['rows'][0]['build_cache_key'], failed_row['build_cache_key'])
            self.assertEqual(engine.state()['budgets']['baseline']['evaluations'], 2)
            again = engine.prepare_baselines(depth='quick')
            self.assertIn(failed_row['result_id'], again['metrics']['cached_result_ids'])
            self.assertEqual(engine.state()['budgets']['baseline']['evaluations'], 2)
            self.assertIn(rid, engine.state()['results'])
            artifact = engine.root / 'results' / failed_row['result_id'] / 'mutable-evidence.zip'
            artifact.chmod(0o600)
            artifact.write_bytes(artifact.read_bytes() + b'tampered')
            with self.assertRaises(Error) as ex:
                engine.prepare_baselines(depth='quick')
            self.assertEqual(ex.exception.code, 'stale_workload_evidence_digest')

    def test_mutable_import_must_fit_one_protocol_request_before_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            card = create_dataset(root / 'data', 'mutable_store')
            manifest = load(card.parent / 'manifest.json')
            record = manifest['objects'][0]
            path = card.parent / record['path']
            bundle = relational.unpack(path.read_bytes())
            tables = [(name, b'3,' + b'x' * 800_000 + b'\n1,\\N\n5,empty\n' if name == 'entities' else data)
                      for name, data in bundle.tables]
            path.write_bytes(relational.pack(bundle.schema_bytes, tables))
            record.update(canonical_bytes=path.stat().st_size, canonical_sha256=sha(path))
            save(card.parent / 'manifest.json', manifest)
            with self.assertRaises(Error) as ex:
                dataset.read_source(card)
            self.assertEqual(ex.exception.code, 'mutable_protocol_request_limit')


if __name__ == '__main__':
    unittest.main()
