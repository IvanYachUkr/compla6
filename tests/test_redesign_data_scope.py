"""Optimization must narrow screen I/O, never the independent full gate."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from compression_lab import baselines, dataset
from compression_lab.engine import Engine
from compression_lab.util import Error
from test_engine import data

class DataScopeTests(unittest.TestCase):
    def setup_workspace(self, root):
        e=Engine.init(root/'work', data(root/'data'))
        _,rows=e.data()
        return e, rows

    def test_manifest_brief_defers_payload_hashing_but_full_rejects_changed_development(self):
        self.assertTrue(hasattr(Engine, 'metadata'), 'need explicit sealed-metadata interface')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); e,rows=self.setup_workspace(root)
            dev=next(r['source'] for r in rows if r['split']=='development')
            dev.chmod(0o644); b=bytearray(dev.read_bytes());b[0]^=1;dev.write_bytes(b)
            brief=e.brief()['metrics']
            self.assertEqual(brief['composition']['development']['objects'],1)
            with self.assertRaises(Error):e.data()
            # Source registration is not a statement about payload integrity.
            reg=e.register(baselines.create(root/'candidate','stored',0))
            screened=e.evaluate(reg['candidate_digest'],'screen',wait=True)
            self.assertTrue(screened['metrics']['screen_passed'],screened)
            self.assertEqual(screened['metrics']['screening']['development_bytes_used'],0)
            full=e.evaluate(reg['candidate_digest'],'full',wait=True)
            self.assertFalse(full['metrics']['quality_passed'])
            self.assertIn('stale_data_digest',full['reason_codes'])
            self.assertEqual(e.state()['budgets']['agent']['evaluations'],2)

    def test_screen_verifies_actual_selected_training_bytes(self):
        self.assertTrue(hasattr(Engine, 'metadata'))
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); e,rows=self.setup_workspace(root)
            reg=e.register(baselines.create(root/'candidate','stored',0))
            train=next(r['source'] for r in rows if r['split']=='train')
            train.chmod(0o644);b=bytearray(train.read_bytes());b[0]^=1;train.write_bytes(b)
            result=e.evaluate(reg['candidate_digest'],'screen',wait=True)
            self.assertFalse(result['metrics']['screen_passed'])
            self.assertIn('stale_data_digest',result['reason_codes'])

    def test_dictionary_training_rejects_tampered_train_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'train'; b=b'abcd'*20000;source.write_bytes(b)
            rows=[dict(alias='train',group='g',split='train',source=source,
                       canonical_sha256=hashlib.sha256(b).hexdigest(),canonical_bytes=len(b))]
            source.write_bytes(b'X'+b[1:])
            with self.assertRaises(Error):baselines.training_samples(rows)
