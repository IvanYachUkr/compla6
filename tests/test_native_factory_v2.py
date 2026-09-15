"""Bounded pinned-native integration canaries, never official timing evidence.

Requires the evaluator-owned COMPRESSION_LAB_NATIVE_PREFIX. Set
COMPRESSION_LAB_NATIVE_TEST_EVIDENCE to retain compact machine-readable checks.
"""
import os
from pathlib import Path
import random
import tempfile
import unittest

from compression_lab import candidate, dataset, gates, native_baselines
from compression_lab.stream import ARCHIVE_MAGIC, StreamRecord, pack_stream
from compression_lab.util import Error, load, save, sha


@unittest.skipUnless(os.environ.get('COMPRESSION_LAB_NATIVE_PREFIX'), 'pinned native prefix not configured')
class NativeFactoryV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='clab-native-factory-tests-')
        cls.root = Path(cls.temp.name)
        cls.work = cls.root/'work'; cls.work.mkdir()
        cls.cache = {}
        cls.diagnostics = {}
        cls.evidence = Path(os.environ['COMPRESSION_LAB_NATIVE_TEST_EVIDENCE']) if os.environ.get('COMPRESSION_LAB_NATIVE_TEST_EVIDENCE') else None
        if cls.evidence:
            cls.evidence.mkdir(parents=True, exist_ok=True)
        cls.records = (
            StreamRecord('a-empty', b''),
            StreamRecord('b-bytes', bytes(range(256))*3),
            StreamRecord('c-random', random.Random(20260907).randbytes(65537)),
            StreamRecord('d-text', b'bounded native factory exactness\r\n'*1000),
            StreamRecord('e-zeros', b'\0'*8193),
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def prepared(self, family, dictionary=False):
        key = family + ('-dictionary' if dictionary else '')
        if key in self.cache:
            return self.cache[key]
        base = self.root/key; base.mkdir()
        rows = []
        if dictionary:
            path = base/'train.bin'
            path.write_bytes(b''.join(
                (f'row={i:06};type={i%17};category={i%11};value={i*i};constant=training-fixture\n').encode()
                for i in range(12000)))
            rows = [{'source': str(path), 'split': 'train', 'alias': 'synthetic-training', 'canonical_sha256': sha(path)}]
        source = native_baselines.create(base/'source', family, 0 if family == 'stored' else 1,
                                         rows=rows, use_dictionary=dictionary, max_threads=4)
        registration = candidate.register(self.work, source, train_hashes=[r['canonical_sha256'] for r in rows])
        registered = self.work/'candidates'/registration['candidate_digest']
        manifest, registration = candidate.verify(registered)
        result = (base, source, registered, manifest, registration)
        self.cache[key] = result
        return result

    def evaluation(self, registered, base, name, threads=1):
        card = dataset.example_card('native-factory-fixture')
        card['limits'].update(threads=threads, timeout_seconds=30)
        card['search_budget']['wall_seconds'] = 180
        evaluation = gates.Evaluation(registered, [], card, base/(name+'-evidence'), depth='quick')
        evaluation.tmp = base/(name+'-calls'); evaluation.tmp.mkdir()
        return evaluation

    def prepared_diagnostic(self, base, source, manifest):
        if base not in self.diagnostics:
            build = candidate.build(source, manifest, base/'diagnostic-build', sanitizer=True)
            candidate.make_runtime(source, base/'diagnostic-build', manifest, build, base/'diagnostic-runtime')
            candidate.make_runtime(source, base/'diagnostic-build', manifest, build, base/'diagnostic-decoder-runtime', decoder=True)
            self.diagnostics[base] = build
        return self.diagnostics[base]

    def retain(self, name, value):
        if self.evidence:
            for key in ('normal', 'diagnostic'):
                if key in value:
                    value[key].update(status='bounded_canary_passed', eligible=False,
                                      reason_codes=['integration_canary_not_full_evaluation'])
            save(self.evidence/(name+'.json'), value)

    def test_all_families_normal_and_diagnostic_exact_corrupt_and_decoder_only(self):
        for family in native_baselines.LIBS:
            with self.subTest(family=family):
                base, source, registered, manifest, registration = self.prepared(family)
                files = {row['path'] for row in registration['decoder_runtime_files']}
                self.assertIn('decoder', files)
                self.assertIn(candidate.DECODER_COMMAND_FILE, files)
                self.assertNotIn('codec', files)
                self.assertFalse(any(path.startswith('vendor/') for path in files))
                self.assertFalse(any(not row['platform'] for row in registration['decoder_dependencies']['libraries']))
                self.assertEqual(manifest['hypothesis']['chunk_bytes'], 4194304)
                normal = self.evaluation(registered, base, 'normal')
                encoded = normal.exact(self.records, family)
                normal.parity(self.records, encoded, 'normal')
                normal.corruption(encoded)
                fixed = candidate.costs(manifest, registration)['decoder']
                self.assertEqual(sum(row['bytes'] for row in registration['decoder_runtime_files']),
                                 sum(fixed[k] for k in ('compiled_decoder_bytes', 'required_decoder_artifact_bytes', 'nonplatform_decoder_dependency_bytes')))
                diagnostic_build = self.prepared_diagnostic(base, source, manifest)
                for dep in (diagnostic_build['dependencies'], diagnostic_build['decoder_dependencies']):
                    names = {row['soname'] for row in dep['libraries']}
                    self.assertTrue(any(name.startswith('libasan.so') for name in names))
                    self.assertTrue(any(name.startswith('libubsan.so') for name in names))
                diagnostic = self.evaluation(registered, base, 'diagnostic')
                diagnostic.runtime = base/'diagnostic-runtime'
                diagnostic.decoder_runtime = base/'diagnostic-decoder-runtime'
                diagnostic.dep = diagnostic_build['dependencies']
                diagnostic.decoder_dep = diagnostic_build['decoder_dependencies']
                diagnostic.sanitizer = True
                sanitized = diagnostic.exact(self.records, family+'-diagnostic')
                self.assertEqual(sanitized, encoded)
                diagnostic.parity(self.records, sanitized, 'diagnostic')
                diagnostic.corruption(sanitized, sanitized=True)
                candidate.verify(registered)
                self.retain(family, {'status': 'passed', 'scope': 'tiny integration canary; no official timing',
                    'candidate_digest': registration['candidate_digest'], 'build_assets': load(source/'build-assets.json'),
                    'decoder_costs': fixed, 'diagnostic_build': diagnostic_build,
                    'normal': normal.raw, 'diagnostic': diagnostic.raw})

    def test_dictionary_training_registration_and_all_four_commands(self):
        base, source, registered, manifest, registration = self.prepared('zstd', dictionary=True)
        self.assertEqual(manifest['training']['kind'], 'train-only')
        self.assertEqual(len(manifest['training']['object_sha256']), 1)
        dictionary_bytes = (source/'dictionary.bin').stat().st_size
        self.assertGreater(dictionary_bytes, 0)
        self.assertLessEqual(dictionary_bytes, 65536)
        for operation, command in manifest['commands'].items():
            self.assertEqual(command[-1], '{runtime}/dictionary.bin', operation)
        evaluation = self.evaluation(registered, base, 'dictionary')
        encoded = evaluation.exact(self.records, 'dictionary')
        evaluation.parity(self.records, encoded, 'dictionary')
        evaluation.corruption(encoded, sanitized=True)
        files = {row['path']: row['bytes'] for row in registration['decoder_runtime_files']}
        self.assertEqual(files['dictionary.bin'], dictionary_bytes)
        candidate.costs(manifest, registration)
        with self.assertRaises(Error) as caught:
            candidate.register(self.work, source, train_hashes=[])
        self.assertEqual(caught.exception.code, 'training_split_violation')
        self.retain('zstd-dictionary', {'status': 'passed', 'dictionary_bytes': dictionary_bytes,
            'training': manifest['training'], 'candidate_digest': registration['candidate_digest'], 'normal': evaluation.raw})

    def test_five_chunk_archives_identical_at_one_two_four_threads(self):
        base, source, registered, manifest, registration = self.prepared('zstd')
        chunk = 4194304
        random_block = random.Random(451).randbytes(65536)
        block = b'fixed deterministic multichunk fixture\n'*100 + random_block
        size = 4*chunk + 17
        payload = (block*((size+len(block)-1)//len(block)))[:size]
        records = (StreamRecord('five-chunks', payload),)
        archives = []; observations = []
        for threads in (1, 2, 4):
            evaluation = self.evaluation(registered, base, 'parallel-'+str(threads), threads)
            encoded = evaluation.exact(records, 'parallel')
            archives.append(pack_stream(ARCHIVE_MAGIC, encoded))
            self.assertEqual(int.from_bytes(encoded[0].payload[12:16], 'big'), 5)
            for row in evaluation.raw['invocations']:
                self.assertEqual(row['threads'], threads)
                self.assertLessEqual(row['max_observed_native_tasks'], threads)
                self.assertEqual(row['returncode'], 0)
            observations.append({'threads': threads, 'invocations': evaluation.raw['invocations']})
        self.assertEqual(archives[0], archives[1])
        self.assertEqual(archives[0], archives[2])
        diagnostic = self.evaluation(registered, base, 'parallel-diagnostic-four', 4)
        diagnostic_build = self.prepared_diagnostic(base, source, manifest)
        diagnostic.runtime = base/'diagnostic-runtime'
        diagnostic.decoder_runtime = base/'diagnostic-decoder-runtime'
        diagnostic.dep = diagnostic_build['dependencies']
        diagnostic.decoder_dep = diagnostic_build['decoder_dependencies']
        diagnostic.sanitizer = True
        encoded = diagnostic.exact(records, 'parallel-diagnostic')
        self.assertEqual(pack_stream(ARCHIVE_MAGIC, encoded), archives[0])
        observations.append({'threads': 4, 'diagnostic': True, 'invocations': diagnostic.raw['invocations']})
        import hashlib
        self.retain('parallel-one-two-four', {'status': 'passed', 'canonical_bytes': size,
            'chunk_count': 5, 'archive_bytes': len(archives[0]), 'archive_sha256': hashlib.sha256(archives[0]).hexdigest(),
            'candidate_digest': registration['candidate_digest'], 'profiles': observations,
            'scope': 'single deterministic canary per profile; not performance evidence'})


if __name__ == '__main__':
    unittest.main()
