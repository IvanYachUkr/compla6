"""Screening/reporting may add information, never relax certification or hide costs."""
import copy
import importlib
import random
import unittest
from compression_lab.util import Error
from compression_lab import accounting, dataset

class SelectionTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('compression_lab.screening'), 'train-only screening module must exist')
        return importlib.import_module('compression_lab.screening')

    def test_selection_is_whole_train_only_bounded_and_order_independent(self):
        api = self.api()
        rows = [dict(alias=f'a-{i}', group=f'g-{i//2}', split='train' if i < 8 else 'development',
                     canonical_bytes=10+i, canonical_sha256=f'{i:064x}') for i in range(10)]
        policy = dict(schema_version=1, max_objects=4, max_canonical_bytes=60, seed=71)
        selected, info = api.select_rows(rows, policy)
        self.assertTrue(selected)
        self.assertTrue(all(r['split']=='train' for r in selected))
        self.assertLessEqual(len(selected), 4)
        self.assertLessEqual(sum(r['canonical_bytes'] for r in selected), 60)
        shuffled = rows[:]; random.Random(9).shuffle(shuffled)
        again, report = api.select_rows(shuffled, policy)
        self.assertEqual(selected, again)
        self.assertEqual(info['selection_digest'], report['selection_digest'])
        self.assertEqual(info['scope'], 'train-only-whole-objects')

    def test_oversized_objects_are_not_sliced_or_replaced_by_development(self):
        api = self.api()
        rows = [dict(alias='train',group='g',split='train',canonical_bytes=100,canonical_sha256='0'*64),
                dict(alias='dev',group='d',split='development',canonical_bytes=1,canonical_sha256='1'*64)]
        with self.assertRaisesRegex(Error, 'screen_no_whole_object_fits'):
            api.select_rows(rows, dict(schema_version=1,max_objects=2,max_canonical_bytes=10,seed=1))

    def test_policy_rejects_booleans_unknown_fields_and_unbounded_work(self):
        api = self.api()
        for change in ({'max_objects':True},{'max_objects':10000000},{'max_canonical_bytes':0}, {'test_split':True}):
            with self.subTest(change=change), self.assertRaises(Error):
                api.validate_policy(dict(schema_version=1,max_objects=8,max_canonical_bytes=1024,seed=1,**change) if not set(change)&{'max_objects','max_canonical_bytes'} else {**dict(schema_version=1,max_objects=8,max_canonical_bytes=1024,seed=1),**change})

class DeploymentTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('compression_lab.deployment'), 'versioned deployment reports must exist')
        return importlib.import_module('compression_lab.deployment')

    def test_exact_matching_and_complete_standalone_are_preserved(self):
        api = self.api()
        lib=dict(soname='libx.so.1',sha256='a'*64,bytes=100,platform=False)
        fixed=dict(fixed_bytes=10,packed_source_bytes=20,config_bytes=30,binary_bytes=40,
                   nonplatform_dependency_bytes=100,dependency_inventory={'libraries':[lib]})
        costs={'development':accounting.costs(1000,100,fixed,100000)}
        original=copy.deepcopy(costs)
        profiles=dict(schema_version=1,profiles=[dict(id='installed-x-v1',libraries=[dict(soname='libx.so.1',sha256='a'*64)])])
        report=api.views(fixed,costs,profiles)
        self.assertEqual(costs,original)
        self.assertEqual(report['primary_scenario'],'standalone-v1')
        self.assertEqual(report['standalone'],costs)
        installed=report['installed']['installed-x-v1']
        self.assertEqual(installed['assumed_installed_bytes'],100)
        self.assertEqual(installed['partitions']['development']['actual_total_bytes'],180)
        profiles['profiles'][0]['libraries'][0]['sha256']='b'*64
        mismatch=api.views(fixed,costs,profiles)['installed']['installed-x-v1']
        self.assertEqual(mismatch['assumed_installed_bytes'],0)
        self.assertEqual(mismatch['partitions']['development']['actual_total_bytes'],280)

    def test_profiles_must_be_declared_before_snapshot_and_strictly_validated(self):
        api=self.api()
        card=dataset.example_card('test')
        card['runtime_scenarios']=dict(schema_version=1,profiles=[dict(id='host-v1',libraries=[dict(soname='libzstd.so.1',sha256='a'*64)])])
        self.assertIs(dataset.validate(card),card)
        for bad in ({'schema_version':2,'profiles':[]}, {'schema_version':1,'profiles':[{'id':'standalone-v1','libraries':[]}]},
                    {'schema_version':1,'profiles':[{'id':'x','libraries':[{'soname':'/tmp/x','sha256':'bad'}]}]}):
            with self.subTest(bad=bad),self.assertRaises(Error): api.validate_scenarios(bad)


class TrainingSampleTests(unittest.TestCase):
    def test_dictionary_sampling_is_bounded_deterministic_and_train_only(self):
        import tempfile
        from pathlib import Path
        from compression_lab import baselines
        self.assertTrue(hasattr(baselines, 'training_samples'))
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rows=[]
            for i in range(4):
                p=root/str(i);p.write_bytes(bytes([i])*65536)
                rows.append(dict(alias='a-'+str(i),source=p,split='train',canonical_sha256=__import__('hashlib').sha256(p.read_bytes()).hexdigest()))
            rows.append(dict(alias='never-open',source=root/'DOES-NOT-EXIST',split='development',canonical_sha256='f'*64))
            chunks,metadata=baselines.training_samples(rows,max_sample_bytes=65536)
            self.assertEqual(sum(map(len,chunks)),65536)
            self.assertEqual(metadata['actual_sample_bytes'],65536)
            self.assertEqual(metadata['sample_cap_bytes_total'],65536)
            self.assertEqual((chunks,metadata),baselines.training_samples(list(reversed(rows)),max_sample_bytes=65536))
            self.assertNotIn('f'*64,metadata['object_sha256'])
