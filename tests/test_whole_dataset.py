"""The unsplit option must never silently score only a validation subset."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import candidate, dataset
from compression_lab.util import Error, load, save, sha
from compression_lab.workloads import bridge
from test_engine import data


def corpus_card(root):
    path = data(root)
    card = load(path)
    card.update(evaluation_mode='whole_dataset', accounting_policy='supervisor-decoder-v1')
    card.pop('development_policy', None)
    card['timing_policy']['scope'] = 'whole-dataset-v1'
    manifest = load(root / 'manifest.json')
    for row in manifest['objects']:
        row['split'] = 'corpus'
    save(root / 'manifest.json', manifest)
    save(path, card)
    return path


class WholeDatasetContract(unittest.TestCase):
    def test_default_split_behavior_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            card, rows = dataset.read_source(data(Path(directory) / 'data'))
            self.assertEqual(dataset.public_partitions(card), ('train', 'development'))
            self.assertEqual(dataset.fitting_partition(card), 'train')
            self.assertEqual(dataset.scored_partition(card), 'development')
            self.assertEqual({r['split'] for r in rows}, {'train', 'development'})

    def test_whole_mode_accepts_corpus_without_train_or_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            card, rows = dataset.read_source(corpus_card(Path(directory) / 'data'))
            self.assertEqual(len(rows), 2)
            self.assertEqual(dataset.public_partitions(card), ('corpus',))
            self.assertEqual(dataset.fitting_partition(card), 'corpus')
            self.assertEqual(dataset.scored_partition(card), 'corpus')
            self.assertEqual(sum(r['canonical_bytes'] for r in rows), 21120)

    def test_mixed_partitions_and_private_data_are_rejected(self):
        for bad_split in ['train', 'development', 'test', 'owner_test']:
            with self.subTest(split=bad_split), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'data'
                path = corpus_card(root)
                manifest = load(root / 'manifest.json')
                manifest['objects'][1]['split'] = bad_split
                save(root / 'manifest.json', manifest)
                with self.assertRaises(Error):
                    dataset.read_source(path)

    def test_mode_and_timing_must_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            card = load(corpus_card(Path(directory) / 'data'))
            for changes in [
                {'evaluation_mode': 'unknown'},
                {'evaluation_mode': 'split'},
                {'timing_policy': {**card['timing_policy'], 'scope': 'validation-only-v2'}},
                {'accounting_policy': 'standalone-v1'},
                {'development_policy': {'training': 'train-only'}},
                {'workload': 'mutable_store'},
            ]:
                with self.subTest(changes=changes), self.assertRaises(Error):
                    dataset.validate({**copy.deepcopy(card), **changes})

    def test_whole_artifacts_are_limited_to_declared_corpus(self):
        manifest = {'artifact_paths': ['dict.bin'],
                    'training': {'kind': 'whole-dataset', 'object_sha256': ['in-corpus']}}
        candidate.validate_training(manifest, ['in-corpus'], 'whole_dataset')
        with self.assertRaises(Error):
            candidate.validate_training(manifest, ['different-corpus'], 'whole_dataset')
        with self.assertRaises(Error):
            candidate.validate_training(manifest, ['in-corpus'], 'split')
        manifest['training']['kind'] = 'train-only'
        with self.assertRaises(Error):
            candidate.validate_training(manifest, ['in-corpus'], 'whole_dataset')

    def test_whole_ranking_uses_actual_corpus_and_charges_decoder_artifacts(self):
        raw = {'evaluation_mode': 'whole_dataset', 'timing_scope': 'whole-dataset-v1',
               'accounting_policy': 'supervisor-decoder-v1',
               'decoder_accounting': {'corpus': {'actual': {
                   'archive_bytes': 800, 'compiled_decoder_bytes': 100,
                   'required_decoder_artifact_bytes': 5000, 'nonplatform_decoder_dependency_bytes': 7,
                   'deployment_total_bytes': 5907}},
                   'development': {'actual': {'deployment_total_bytes': 1}}}}
        self.assertEqual(bridge.rank_cost(raw), 5907)
        self.assertEqual(bridge.decoder_accounting_metrics(raw)['required_decoder_artifact_bytes'], 5000)
        metrics = bridge.result_metrics(raw)
        self.assertIn('decoder_accounting_corpus_actual', metrics)
        self.assertNotIn('decoder_accounting_development_actual', metrics)

    def test_brief_reports_corpus_and_permits_specialization(self):
        from compression_lab.engine import Engine
        from compression_lab.research import brief
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine.init(root / 'work', corpus_card(root / 'data'), exploratory=True)
            report = brief(engine)
            self.assertEqual(set(report['composition']), {'corpus'})
            self.assertEqual(report['composition']['corpus']['canonical_bytes'], 21120)
            self.assertEqual(report['evaluation_mode'], 'whole_dataset')
            self.assertIn('complete supplied corpus', report['search_contract'][0])
            self.assertEqual(engine.profile()['metrics']['evaluation_mode'], 'whole_dataset')

    def test_screen_selects_corpus_objects_without_inventing_a_train_partition(self):
        from compression_lab.screening import select_rows
        with tempfile.TemporaryDirectory() as directory:
            card, rows = dataset.read_source(corpus_card(Path(directory) / 'data'))
            selected, report = select_rows(rows, fitting_partition=dataset.fitting_partition(card))
            self.assertEqual(len(selected), 2)
            self.assertEqual(report['scope'], 'corpus-whole-objects')
            self.assertEqual(report['corpus_groups'], 2)
            self.assertNotIn('training_groups', report)

    def test_whole_pair_explains_actual_decoder_score_including_large_artifact(self):
        from compression_lab import accounting
        from test_paired_diagnostics import result
        reference, trial = result([10, 100, 30]), result([5, 90, 25])
        for raw, artifact in ((reference, 0), (trial, 5000)):
            raw.update(evaluation_mode='whole_dataset', timing_scope='whole-dataset-v1')
            for row in raw['objects']:
                row['split'] = 'corpus'
            n = sum(r['canonical_bytes'] for r in raw['objects'])
            a = sum(r['archive_bytes'] for r in raw['objects']) + 9
            raw['accounting'] = {'corpus': accounting.costs(n, a, raw['fixed_costs'], 1000000)}
            raw['fixed_costs']['decoder'] = {'compiled_decoder_bytes': 100,
                'required_decoder_artifact_bytes': artifact, 'nonplatform_decoder_dependency_bytes': 7}
            raw['fixed_costs']['installed_runtime_policy'] = 'test'
            raw['decoder_accounting'] = {'corpus': accounting.decoder_costs(n, a, raw['fixed_costs'], 1000000)}
        report = accounting.paired_diagnostics(reference, trial)
        self.assertEqual(report['split'], 'corpus')
        self.assertEqual(report['objects'], 3)
        self.assertEqual(report['corpus_archive_delta_bytes'], -20)
        self.assertEqual(report['deployment_delta_bytes'], 4980)
        self.assertEqual(report['deployment_components_delta_bytes']['required_decoder_artifact_bytes'], 5000)
        self.assertNotIn('development_archive_delta_bytes', report)
        self.assertIn('whole-dataset-v1', report['timing_scope'])


class WholeDatasetNative(unittest.TestCase):
    def test_full_native_dictionary_evaluation_times_and_accounts_for_every_corpus_byte(self):
        import zipfile
        from compression_lab.engine import Engine
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = corpus_card(root / 'data')
            manifest = load(root / 'data/manifest.json')
            for row in manifest['objects']:
                source = root / 'data' / row['path']
                source.write_bytes(source.read_bytes() * 64)
                row.update(canonical_bytes=source.stat().st_size, canonical_sha256=sha(source))
            save(root / 'data/manifest.json', manifest)
            n = sum(r['canonical_bytes'] for r in manifest['objects'])
            engine = Engine.init(root / 'work', path)
            evaluated = engine.baseline_trial('parallel-zstd-1-dict64k', 'full', wait=True)
            self.assertTrue(evaluated['metrics']['quality_passed'], evaluated)
            result_id = evaluated['metrics']['result_id']
            raw = engine.raw(result_id)
            self.assertEqual({r['split'] for r in raw['objects']}, {'corpus'})
            self.assertEqual(set(raw['decoder_accounting']), {'all', 'corpus'})
            self.assertEqual(raw['decoder_accounting']['corpus']['canonical_bytes'], n)
            for phase in ('encode', 'decode'):
                self.assertEqual(len(raw['timing_trials'][phase]), 7)
                self.assertTrue(all(t['canonical_bytes'] == n for t in raw['timing_trials'][phase]))
            costs = raw['decoder_accounting']['corpus']['actual']
            self.assertGreater(costs['required_decoder_artifact_bytes'], 0)
            self.assertEqual(bridge.rank_cost(raw), costs['deployment_total_bytes'])
            registered = engine.root / 'candidates' / evaluated['candidate_digest']
            self.assertEqual(load(registered / 'source/candidate.json')['training']['kind'], 'whole-dataset')
            actual_files = [p for p in (registered / 'decoder-runtime').rglob('*') if p.is_file()]
            self.assertEqual(sum(p.stat().st_size for p in actual_files),
                costs['compiled_decoder_bytes'] + costs['required_decoder_artifact_bytes'] +
                costs['nonplatform_decoder_dependency_bytes'])
            feedback = engine.feedback(result_id)['metrics']
            self.assertEqual(feedback['scored_partition'], 'corpus')
            self.assertNotIn('development', feedback)
            output = root / 'result.zip'
            engine.export(result_id, output)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertTrue(any(name.startswith('candidate/decoder-runtime/') for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
