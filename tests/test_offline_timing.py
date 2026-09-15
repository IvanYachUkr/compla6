"""Offline preparation must be measured, reproduced and included in total time."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

from compression_lab import accounting, baselines, candidate, dataset, gates, native_baselines
from compression_lab.util import Error, load, save, sha


class StageTimingTests(unittest.TestCase):
    def test_total_is_the_median_of_paired_trials_not_the_sum_of_medians(self):
        self.assertTrue(hasattr(accounting, 'combine_encoding_trials'),
                        'The evaluator needs explicit paired offline/online accounting')
        offline = [{'trial':i+1, 'elapsed_ns':int(t*1e9), 'canonical_bytes':50,
                    'peak_rss_bytes':20} for i,t in enumerate((.1,.2,.3,.4,.5,.6,100))]
        online = [{'trial':i+1, 'elapsed_ns':int(t*1e9), 'canonical_bytes':100,
                   'peak_rss_bytes':10} for i,t in enumerate((10,.1,.1,.1,.1,.1,.1))]
        combined = accounting.combine_encoding_trials(offline, online)
        self.assertEqual([r['elapsed_ns'] for r in combined],
                         [10100000000,300000000,400000000,500000000,600000000,700000000,100100000000])
        self.assertEqual(accounting.timing(combined, 'combined')['median_seconds'], .6)
        self.assertTrue(all(r['canonical_bytes']==100 and r['peak_rss_bytes']==20 for r in combined))
        self.assertEqual([r['elapsed_ns'] for r in accounting.combine_encoding_trials([], online)],
                         [r['elapsed_ns'] for r in online])
        with self.assertRaises(Error):
            accounting.combine_encoding_trials(offline[:-1], online)

    def test_new_cards_require_separate_stage_accounting_and_preserve_legacy_cards(self):
        card = dataset.research_card('staged')
        self.assertEqual(card['timing_policy'].get('operation'), 'offline-plus-online-v1')
        self.assertEqual(card['timing_policy'].get('encoding_floor_scope'), 'combined')
        legacy = copy.deepcopy(card)
        legacy['timing_policy']['encoding_floor_scope'] = 'online'
        self.assertEqual(dataset.validate(legacy), legacy)
        self.assertNotIn('operation', dataset.example_card('legacy')['timing_policy'])
        broken = copy.deepcopy(card)
        broken['timing_policy']['encoding_floor_scope'] = 'unmeasured'
        with self.assertRaises(Error): dataset.validate(broken)


@unittest.skipUnless(os.environ.get('COMPRESSION_LAB_NATIVE_PREFIX'), 'pinned native prefix required')
class OfflineNativeTests(unittest.TestCase):
    def test_shared_dictionary_replay_charges_rebuild_and_requires_a_declared_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); files=[]
            for part in range(2):
                path = root/f'input-{part}'
                path.write_bytes(b''.join(f'event {i%31} user {i%127} value {part}-{i*i}\n'.encode()
                                         for i in range(5000)))
                files.append(path)
            card_path = dataset.create_research_input(root/'data', files, 'shared-fit', smoke=True)
            card, rows = dataset.read_source(card_path)
            cpus = [max(os.sched_getaffinity(0))]
            card['resource_profiles'] = {'schema_version':1, 'primary':'preparation',
                'profiles':[{'id':'preparation', 'threads':1, 'cpus':cpus}]}
            card['limits']['cpus'] = cpus
            source = baselines.create(root/'source', 'zstd', 1, rows, True,
                                      fitting_partition='corpus', offline_replay=True)
            manifest = load(source/'candidate.json'); manifest['offline']['rebuild'] = True
            save(source/'candidate.json', manifest)
            workspace = root/'work'; workspace.mkdir()
            registered = candidate.register(workspace, source, train_hashes=[r['canonical_sha256'] for r in rows],
                                            evaluation_mode='whole_dataset')
            evaluation = gates.Evaluation(workspace/'candidates'/registered['candidate_digest'], rows, card, root/'evidence')
            evaluation.tmp = root/'calls'; evaluation.tmp.mkdir()
            prepared, measured = evaluation.prepare_offline()
            self.assertGreater(measured['build_elapsed_ns'], 0)
            self.assertEqual(measured['elapsed_ns'], measured['preparation_elapsed_ns']+measured['build_elapsed_ns'])
            self.assertEqual(sha(prepared/'dictionary.bin'), sha(source/'dictionary.bin'))
            self.assertEqual(sha(prepared/'codec'), sha(evaluation.root/'runtime/codec'))
            builds = [r for r in evaluation.raw['invocations'] if r['phase']=='offline_build']
            self.assertTrue(builds)
            self.assertTrue(all(r['cpus']==cpus for r in builds), builds)
            # Required provenance cannot silently inherit a fit-excluded timing result.
            evaluation.m.pop('offline')
            rejected = evaluation.run()
            self.assertIn('offline_preparation_required', rejected['reason_codes'])

    def test_full_gate_regenerates_dictionary_and_reports_both_measured_stages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); files=[]
            for part in range(2):
                file = root/f'input-{part}'
                file.write_bytes(b''.join(
                    f'{{"user":{i%97},"event":{i%13},"message":"measured fitting {part}-{i%173}","value":{i*i}}}\n'.encode()
                    for i in range(5000)))
                files.append(file)
            cp = dataset.create_research_input(root/'data', files, 'offline-canary', smoke=True)
            card, rows = dataset.read_source(cp)
            # Call the actual factory and native gates without a model or campaign.
            try:
                source = native_baselines.create(root/'source', 'zstd', 1, rows, True,
                    max_threads=1, fitting_partition='corpus', offline_replay=True)
            except TypeError as error:
                self.fail('Native fitted candidates need a reproducible offline stage: '+str(error))
            manifest = load(source/'candidate.json')
            self.assertIn('offline', manifest)
            workspace = root/'work'; workspace.mkdir()
            registration = candidate.register(workspace, source, train_hashes=[r['canonical_sha256'] for r in rows],
                                              evaluation_mode='whole_dataset')
            registered = workspace/'candidates'/registration['candidate_digest']
            raw = gates.Evaluation(registered, rows, card, root/'evidence').run()
            self.assertTrue(raw['quality_passed'], raw.get('error'))
            self.assertEqual(raw['encoding_floor_scope'], 'combined')
            self.assertEqual(len(raw['timing_trials']['offline']), 7)
            self.assertEqual(len(raw['timing_trials']['encode']), 7)
            self.assertEqual(len(raw['timing_trials']['combined']), 7)
            for offline, online, combined in zip(raw['timing_trials']['offline'],
                    raw['timing_trials']['encode'], raw['timing_trials']['combined']):
                self.assertGreater(offline['elapsed_ns'], 0)
                self.assertEqual(combined['elapsed_ns'], offline['elapsed_ns']+online['elapsed_ns'])
            self.assertGreater(raw['timing']['combined']['median_seconds'], raw['timing']['encode']['median_seconds'])
            self.assertEqual(len(raw['offline_preparation']['artifact_checks']), 8)
            self.assertTrue(raw['gates']['offline_artifact_reproduction'])
            self.assertEqual(sha(source/'dictionary.bin'), sha(registered/'decoder-runtime/dictionary.bin'))

            # An edited model may decode, but it cannot inherit another fitter's timing.
            changed = root/'changed'
            import shutil
            shutil.copytree(source, changed)
            original = (changed/'dictionary.bin').read_bytes()
            (changed/'dictionary.bin').write_bytes(original[:-1]+bytes([original[-1]^1]))
            bad = candidate.register(workspace, changed, train_hashes=[r['canonical_sha256'] for r in rows],
                                     evaluation_mode='whole_dataset')
            evaluation = gates.Evaluation(workspace/'candidates'/bad['candidate_digest'], rows, card, root/'bad-evidence')
            evaluation.tmp = root/'calls'; evaluation.tmp.mkdir()
            with self.assertRaisesRegex(Error, 'offline_artifact_mismatch'):
                evaluation.prepare_offline()


if __name__ == '__main__': unittest.main()
