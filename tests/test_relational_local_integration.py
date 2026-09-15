import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import owner
from compression_lab.util import Error, save
from compression_lab.workloads import bridge, registry, relational
from compression_lab.workloads.fixtures import movie_bundle, movie_schema
from compression_lab.workloads.mutable_cases import StoreCase


class RelationalLocalIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        registry.create_reference(cls.root / 'source')
        cls.registration = registry.register(cls.root, cls.root / 'source')
        cls.candidate = cls.root / 'candidates' / cls.registration['candidate_digest']

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_empty_seed_supports_atomic_insert_replay_and_reopen(self):
        schema = json.dumps(movie_schema()).encode()
        seed = relational.pack(schema, [(t['name'], b'') for t in movie_schema()['tables']])
        with tempfile.TemporaryDirectory() as folder:
            result = StoreCase(self.candidate, seed, Path(folder) / 'case', scenario='basic').run()
        self.assertTrue(result['quality_passed'], result.get('error'))

    def test_seed_with_insert_id_collisions_supports_recovery(self):
        from compression_lab.workloads.fixtures import transaction
        from compression_lab.workloads.oracle import ReferenceState
        state = ReferenceState(movie_bundle())
        for i in range(1, 5):
            state.bundle = state.prepare(transaction(i))
        with tempfile.TemporaryDirectory() as folder:
            result = StoreCase(self.candidate, state.export_bytes(), Path(folder) / 'case',
                               scenario='merge_reopen_insert').run()
        self.assertTrue(result['quality_passed'], result.get('error'))

    def test_owner_copy_preserves_mutable_runtime_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / self.registration['candidate_digest']
            owner.copy_candidate(self.candidate, target, self.registration['candidate_digest'])
            manifest, registration = registry.verify(target)
            self.assertEqual(registration['candidate_digest'], self.registration['candidate_digest'])
            self.assertTrue((target / 'runtime' / manifest['python_runtime']['executable']).stat().st_mode & 0o111)

    def test_private_relational_input_is_validated_after_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bad = b'invalid RLB1'
            (root / 'invalid.rlb').write_bytes(bad)
            save(root / 'manifest.json', {'schema_version': 1, 'objects': [
                {'alias': 'private-0', 'group': 'hidden-0', 'path': 'invalid.rlb',
                 'canonical_bytes': len(bad), 'canonical_sha256': hashlib.sha256(bad).hexdigest()}]})
            save(root / 'private.json', {'schema_version': 1, 'adapter': 'relational_bundle',
                                         'private_manifest': 'manifest.json'})
            with self.assertRaises(Error):
                owner.private_rows(root, {'private_card': 'private.json'},
                                   {'adapter': 'relational_bundle', 'workload': 'mutable_store'})

    def test_quick_compare_without_development_projection(self):
        class E:
            root = Path('.')
            def raw(self, rid):
                return {'card_digest': 'card', 'runtime_digest': 'runtime', 'candidate_digest': rid,
                        'depth': 'quick', 'status': 'ineligible',
                        'accounting': {'development': {'projection': None}}}
            def state(self):
                return {'run_id': 'run-test'}
        with patch.object(bridge, 'verify_candidate', return_value=({'candidate_id': 'tiny'}, {})):
            result = bridge.compare(E(), ['one', 'two'])
        self.assertIsNone(result['metrics']['rows'][0]['deployment_total_bytes'])

    def test_resume_keeps_best_eligible_export_after_failed_revision(self):
        class E:
            def state(self):
                return {'active_job': None, 'stage': 'public_ready', 'run_id': 'run-test',
                        'candidate_digest': 'slow', 'result_id': 'slow-result',
                        'best_eligible_result': 'good-result', 'best_eligible_candidate': 'good',
                        'budgets': {}, 'search_budget': {'candidate_evaluations': 12, 'wall_seconds': 3600}}
            def raw(self, rid):
                return {'candidate_digest': 'slow' if rid == 'slow-result' else 'good',
                        'quality_passed': True, 'depth': 'full', 'eligible': rid == 'good-result'}
        result = bridge.resume(E())['metrics']
        self.assertEqual(result['next_action']['result'], 'good-result')
        self.assertEqual(result['latest_result_id'], 'slow-result')

    def test_measurement_sequences_keep_distinct_lengths_and_merge_counts(self):
        from compression_lab.workloads.cards import measurement_sequences, sequence_name
        from compression_lab.workloads.mutable import summarize
        cases = []
        with tempfile.TemporaryDirectory() as folder:
            for sequence in measurement_sequences({}):
                case = StoreCase(self.candidate, movie_bundle(), Path(folder) / sequence_name(sequence),
                                 scenario='measurement', metric_trial=1,
                                 measurement_sequence=sequence).run()
                self.assertTrue(case['quality_passed'], case.get('error'))
                operations = case['operations']
                self.assertEqual(sum(op['op'] == 'transaction' for op in operations), sequence['transactions'])
                self.assertEqual(sum(op['op'] == 'merge' for op in operations), sequence['merges'])
                cases.append(case)
            result = summarize(cases, {'deployment_fixed_bytes': 100, 'source_primary_fixed_bytes': 200},
                               [{'alias': 'synthetic', 'split': 'development', 'canonical_bytes': len(movie_bundle())}])
        obj = result['objects']['synthetic']
        self.assertEqual(len(obj['measurement_sequences']), 3)
        self.assertEqual(obj['rank_sequence'], 'inserts-16-merges-4')
        self.assertEqual(result['development']['deployment_total_bytes'], obj['median_store_bytes'] + 100)
        self.assertTrue(all(name in '\n'.join(result['operation_latency']) for name in obj['measurement_sequences']))

    def test_private_evidence_keeps_scope_and_public_training_hashes(self):
        import zipfile
        from compression_lab.workloads.mutable import Evaluation
        with tempfile.TemporaryDirectory() as folder:
            evaluation = Evaluation(self.candidate, [], {'search_budget': {'wall_seconds': 60}}, folder,
                                    data_scope='private', train_hashes=['public-training-only'])
            self.assertEqual(evaluation.train_hashes, ['public-training-only'])
            evaluation._evidence()
            with zipfile.ZipFile(Path(folder) / 'mutable-evidence.zip') as archive:
                manifest = json.loads(archive.read('EVIDENCE_MANIFEST.json'))
            self.assertEqual(manifest['data_scope'], 'private')
            self.assertFalse(manifest['public_or_synthetic_only'])


if __name__ == '__main__':
    unittest.main()
