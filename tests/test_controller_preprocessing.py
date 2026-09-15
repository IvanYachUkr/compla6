"""Cancellation and deadline boundaries for registration and trusted training."""
import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from compression_lab import baselines, candidate, native_baselines, runner
from compression_lab.util import Error, load, save, sha
from compression_lab.workloads import registry


class PreprocessingLimits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = self.root / 'cancel.json'
        self.limits = {'_deadline': time.monotonic() + 10, '_cancel': self.cancel,
                       'memory_bytes': 256 * 1024 * 1024}

    def rows(self):
        source = self.root / 'train.bin'
        source.write_bytes(b'train-only source record\n' * 4096)
        return [{'alias': 'train', 'source': source, 'split': 'train', 'canonical_sha256': sha(source)},
                {'alias': 'private-never-read', 'source': self.root / 'absent',
                 'split': 'development', 'canonical_sha256': 'f' * 64}]

    def source(self):
        return baselines.create(self.root / 'source', 'stored', 0)

    def test_factories_reject_cancelled_or_expired_work_before_writing(self):
        for factory in (baselines.create, native_baselines.create):
            for expired in (False, True):
                with self.subTest(factory=factory.__module__, expired=expired):
                    self.cancel.unlink(missing_ok=True)
                    limits = dict(self.limits)
                    if expired: limits['_deadline'] = time.monotonic() - 1
                    else: self.cancel.touch()
                    output = self.root / 'must-not-create'
                    with self.assertRaises(Error) as caught:
                        factory(output, 'stored', 0, limits=limits)
                    self.assertEqual(caught.exception.code, 'timeout' if expired else 'cancelled')
                    self.assertFalse(output.exists())

    def test_baseline_dispatch_passes_limits_to_native_factory(self):
        output = self.root / 'native-source'
        with patch.object(native_baselines, 'create', return_value=output) as create:
            self.assertEqual(baselines.create(output, 'stored', 0, native_parallel=True,
                                             limits=self.limits), output)
        self.assertEqual(create.call_args.kwargs['limits'], self.limits)

    def test_expired_build_does_not_launch_a_compiler(self):
        source = self.source()
        manifest = load(source / 'candidate.json')
        with patch.object(candidate, 'execute', side_effect=AssertionError('compiler started after deadline')):
            with self.assertRaises(Error) as caught:
                candidate.build(source, manifest, self.root / 'build', limits={'_deadline': time.monotonic() - 1})
        self.assertEqual(caught.exception.code, 'timeout')

    def test_build_passes_cancel_and_recomputes_remaining_time_per_command(self):
        source = self.source()
        manifest = load(source / 'candidate.json')
        manifest['build_commands'] = [['g++', 'first'], ['g++', 'second']]
        calls = []
        def execute(argv, **kwargs):
            calls.append(kwargs)
            self.cancel.touch()
            return {}
        with patch.object(candidate, 'execute', side_effect=execute):
            with self.assertRaises(Error) as caught:
                candidate.build(source, manifest, self.root / 'build', limits=self.limits)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['cancel'], self.cancel)
        self.assertGreater(calls[0]['timeout'], 0)
        self.assertLessEqual(calls[0]['timeout'], 10)

    def test_native_registration_passes_cancel_to_build(self):
        source = self.source()
        (self.root / 'work').mkdir()
        with patch.object(candidate, 'fingerprint', return_value={'runtime_digest': 'fixture'}) as fingerprint, \
                patch.object(candidate, 'build', side_effect=Error('intercepted_build')) as build:
            with self.assertRaises(Error) as caught:
                candidate.register(self.root / 'work', source, limits=self.limits)
        self.assertEqual(caught.exception.code, 'intercepted_build')
        self.assertEqual(build.call_args.kwargs.get('cancel'), self.cancel)
        self.assertEqual(fingerprint.call_args.kwargs.get('limits'), self.limits)

    def test_nearly_expired_fingerprint_clamps_probe_and_stops_before_next_probe(self):
        now = [100.0]
        calls = []
        def version_probe(argv, **kwargs):
            calls.append(kwargs)
            now[0] = 100.02
            return type('Probe', (), {'stdout': 'fixture-version\n'})()
        with patch.object(runner.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(runner.subprocess, 'run', side_effect=version_probe):
            with self.assertRaises(Error) as caught:
                runner.fingerprint(limits={'_deadline': 100.01})
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0]['timeout'], 0)
        self.assertLessEqual(calls[0]['timeout'], .010001)

    def test_mutable_native_registration_passes_cancel_to_build(self):
        source = self.source()
        base = load(source / 'candidate.json')
        (source / 'candidate.json').unlink()
        recipe = ('build_commands', 'sanitizer_build_commands', 'build_output_paths',
                  'executable', 'toolchain', 'dependencies')
        manifest = dict(schema_version=1, workload='mutable_store', protocol_version=1,
                        candidate_id='mutable-fixture', language='c++', threads=1, deterministic=True,
                        source_paths=base['source_paths'], artifact_paths=[], runtime_paths=[],
                        command=['{runtime}/codec'], durability_claim='process_crash_only',
                        native_build={key: base[key] for key in recipe})
        save(source / registry.MANIFEST, manifest)
        with patch.object(candidate, 'build', side_effect=Error('intercepted_build')) as build:
            with self.assertRaises(Error) as caught:
                registry.register(self.root / 'work', source, limits=self.limits)
        self.assertEqual(caught.exception.code, 'intercepted_build')
        self.assertEqual(build.call_args.kwargs.get('cancel'), self.cancel)

    def test_sampling_checks_cancellation_during_whole_object_hashing(self):
        rows = self.rows()
        source = rows[0]['source']
        payload = source.read_bytes()
        class CancelAfterRead(io.BytesIO):
            def read(inner, *args):
                result = super().read(*args)
                self.cancel.touch()
                return result
        original = Path.open
        def opened(path, *args, **kwargs):
            return CancelAfterRead(payload) if path == source else original(path, *args, **kwargs)
        with patch.object(Path, 'open', opened):
            with self.assertRaises(Error) as caught:
                baselines.training_samples(rows, limits=self.limits)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_pinned_trainer_build_and_training_share_cancel_and_remaining_deadline(self):
        rows = self.rows()
        prefix = self.root / 'prefix'
        for name in ('include/zdict.h', 'include/zstd.h', 'include/zstd_errors.h', 'lib/libzstd.a'):
            path = prefix / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
        calls = []
        def execute(argv, **kwargs):
            calls.append(kwargs)
            if kwargs.get('build'): (kwargs['output'] / 'trainer').write_bytes(b'fixture')
            else: (kwargs['output'] / 'dictionary.bin').write_bytes(b'dictionary')
            return {}
        with patch.object(native_baselines, 'execute', side_effect=execute), \
                patch.object(candidate, 'closure', return_value={}), \
                patch.object(candidate, 'runtime_mounts', return_value={}):
            value, provenance = native_baselines.train_dictionary(rows, prefix, limits=self.limits)
        self.assertEqual(value, b'dictionary')
        self.assertEqual(len(calls), 2)
        self.assertEqual([call['cancel'] for call in calls], [self.cancel, self.cancel])
        self.assertTrue(all(0 < call['timeout'] <= 10 for call in calls))
        self.assertEqual([call['memory'] for call in calls], [self.limits['memory_bytes']] * 2)
        self.assertEqual(provenance['object_sha256'], [rows[0]['canonical_sha256']])

    def test_source_packages_remain_byte_identical_with_checkpointed_compression(self):
        source = self.root / 'source'; source.mkdir()
        (source / 'codec.c').write_bytes(b'fixed package bytes\n' * 100000)
        (source / 'candidate.json').write_bytes(b'{}\n')
        manifest = {'source_paths': ['codec.c'], 'artifact_paths': [], 'runtime_paths': []}
        old = self.root / 'old.zip'
        with zipfile.ZipFile(old, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in ['candidate.json', 'codec.c']:
                entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                entry.external_attr = 0o100644 << 16; entry.create_system = 3
                archive.writestr(entry, (source / name).read_bytes(),
                                 compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        new = self.root / 'new.zip'
        candidate.source_zip(source, manifest, new, limits=self.limits)
        self.assertEqual(new.read_bytes(), old.read_bytes())

    def test_legacy_training_child_is_killed_after_cancellation(self):
        rows = self.rows()
        marker = self.root / 'child-pid'
        done = threading.Event()
        def cancel_when_started():
            for _ in range(500):
                if done.wait(.01): return
                if marker.exists():
                    self.cancel.touch()
                    return
        watcher = threading.Thread(target=cancel_when_started, daemon=True)
        watcher.start()
        self.addCleanup(lambda: (done.set(), watcher.join(timeout=2)))
        # Real isolated subprocess and teardown, with a sleeping trusted trainer
        # fixture so this tests interruption without a codec benchmark.
        code = 'import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
        with patch.object(baselines, 'libraries', return_value={'libzstd.so.1': marker}), \
                patch.object(baselines, '_DICTIONARY_TRAINER', code, create=True):
            start = time.monotonic()
            with self.assertRaises(Error) as caught:
                baselines.dictionary(rows, limits=self.limits)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertTrue(marker.exists(), 'The child must have started before cancellation')
        self.assertLess(time.monotonic() - start, 5)
        with self.assertRaises(ProcessLookupError): os.kill(int(marker.read_text()), 0)


if __name__ == '__main__':
    unittest.main()
