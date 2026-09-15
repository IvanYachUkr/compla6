"""Small native role-isolation fixtures; these are not complete codec gates."""
import copy
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from compression_lab import accounting, candidate, dataset, gates, runner
from compression_lab.util import Error, load, save, sha


ENCODER = r'''
#include <cstdio>
#include <zstd.h>
int main() {
    if (!ZSTD_versionNumber()) return 60;
    int byte; while ((byte = getchar()) != EOF) putchar(byte);
    return ferror(stdin) || ferror(stdout);
}
'''
DECODER = r'''
#include <cstdio>
#include <string>
#include <zlib.h>
int main(int argc, char** argv) {
    if (argc != 3 || !zlibVersion()) return 61;
    for (int i = 1; i < argc; ++i) {
        FILE* f = fopen(argv[i], "rb");
        if (!f) return 62;
        if (fgetc(f) == EOF) return 63;
        fclose(f);
    }
    std::string input; int byte;
    while ((byte = getchar()) != EOF) input += char(byte);
    if (input == "need-encoder") {
        FILE* f = fopen("/candidate/encoder.tbl", "rb");
        if (!f) return 64;
        fclose(f);
    }
    fwrite(input.data(), 1, input.size(), stdout);
    return ferror(stdin) || ferror(stdout);
}
'''
COMBINED = r'''
#include <cstdio>
int main() {
    int byte; while ((byte = getchar()) != EOF) putchar(byte);
    return ferror(stdin) || ferror(stdout);
}
'''


def source_fixture(root, version=2, combined=False):
    """Pinned direct compilers; different ELF dependencies for each role."""
    root.mkdir()
    compiler = Path(shutil.which('g++', path='/usr/bin:/bin')).resolve()
    binaries = ['codec'] if combined else ['encoder', 'decoder']
    texts = {'codec': COMBINED} if combined else {'encoder': ENCODER, 'decoder': DECODER}
    for name, text in texts.items():
        (root / (name + '.cpp')).write_text(text)
    libraries = candidate.libraries()
    dependencies = []
    if not combined:
        for name, license_name in [('libzstd.so.1', 'BSD-3-Clause'), ('libz.so.1', 'Zlib')]:
            path = libraries[name]
            dependencies.append({'soname': name, 'sha256': sha(path), 'version': 'local-test-hash-pin',
                                 'license': license_name, 'build_path': str(path)})
        for name in ('decoder.dict', 'decoder.cfg', 'encoder.tbl'):
            (root / name).write_bytes((name + '-fixture-data\n').encode())
    normal, diagnostic = [], []
    for name in binaries:
        link = [] if combined else ['-lzstd' if name == 'encoder' else '-lz']
        tail = ['{source}/' + name + '.cpp', *link, '-o', '{build}/' + name]
        normal.append(['g++', '-std=c++17', '-O2', *tail])
        diagnostic.append(['g++', '-std=c++17', '-O1', '-g', '-no-pie',
                           '-fsanitize=address,undefined', '-fno-omit-frame-pointer', *tail])
    encoder, decoder = ('codec', 'codec') if combined else ('encoder', 'decoder')
    commands = {}
    for operation in ('encode_stream', 'decode_stream', 'encode_dir', 'decode_dir'):
        decoding = operation.startswith('decode')
        commands[operation] = ['{runtime}/' + (decoder if decoding else encoder)]
        if decoding and not combined:
            commands[operation] += ['{runtime}/decoder.dict', '{runtime}/decoder.cfg']
    manifest = {'schema_version': version, 'candidate_id': 'decoder-role-canary',
                'language': 'c++', 'input_domain': 'opaque_bytes', 'deterministic': True,
                'threads': 1, 'source_paths': [name + '.cpp' for name in binaries],
                'artifact_paths': [] if combined else ['decoder.dict', 'encoder.tbl'],
                'runtime_paths': [] if combined else ['decoder.cfg'],
                'build_commands': normal, 'sanitizer_build_commands': diagnostic,
                'build_output_paths': binaries, 'executable': encoder, 'commands': commands,
                'toolchain': {'g++': {'path': str(compiler), 'sha256': sha(compiler)}},
                'dependencies': dependencies, 'training': {'kind': 'data-independent'}}
    if version == 2:
        manifest.update(decoder={'executable': decoder, 'build_output_paths': [decoder],
                                 'artifact_paths': [] if combined else ['decoder.dict'],
                                 'runtime_paths': [] if combined else ['decoder.cfg']},
                        diagnostics={'kind': 'cxx-asan-ubsan-v1'})
    save(root / 'candidate.json', manifest)
    return candidate.manifest(manifest)


class DecoderAccountingV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='clab-decoder-v2-tests-')
        cls.root = Path(cls.temp.name)
        cls.work = cls.root / 'work'; cls.work.mkdir()
        cls.source = cls.root / 'source'
        cls.manifest = source_fixture(cls.source)
        cls.record = candidate.register(cls.work, cls.source)
        cls.registered = cls.work / 'candidates' / cls.record['candidate_digest']

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_physical_decoder_inventory_equals_b_a_d_and_excludes_encoder(self):
        manifest, record = candidate.verify(self.registered)
        fixed = candidate.costs(manifest, record)
        files = {row['path']: row['bytes'] for row in record['decoder_runtime_files']}
        self.assertEqual(set(files), {'decoder', 'decoder.dict', 'decoder.cfg', 'lib/libz.so.1', candidate.DECODER_COMMAND_FILE})
        costs = fixed['decoder']
        self.assertEqual(costs['compiled_decoder_bytes'], files['decoder'])
        self.assertEqual(costs['required_decoder_artifact_bytes'], files['decoder.dict'] + files['decoder.cfg'] + files[candidate.DECODER_COMMAND_FILE])
        self.assertEqual(costs['decoder_command_bytes'], files[candidate.DECODER_COMMAND_FILE])
        self.assertEqual(costs['nonplatform_decoder_dependency_bytes'], files['lib/libz.so.1'])
        self.assertEqual(sum(files.values()), sum(costs[key] for key in (
            'compiled_decoder_bytes', 'required_decoder_artifact_bytes', 'nonplatform_decoder_dependency_bytes')))
        self.assertNotIn('libzstd.so.1', {d['soname'] for d in record['decoder_dependencies']['libraries']})
        score = accounting.decoder_costs(100, 73, fixed, 1000)
        self.assertEqual(score['actual']['compressed_plus_decoder_bytes'], 73 + files['decoder'])
        self.assertEqual(score['actual']['deployment_total_bytes'], 73 + sum(files.values()))
        self.assertGreater(fixed['encoder_only_binary_bytes_reported_separately'], 0)
        self.assertGreater(fixed['encoder_only_artifact_bytes_reported_separately'], 0)

    def evaluation(self, root, base):
        card = dataset.example_card('decoder-gate-fixture')
        card['accounting_policy'] = 'supervisor-decoder-v1'
        card['timing_policy']['scope'] = 'validation-only-v2'
        evaluation = gates.Evaluation(root, [], card, base / 'evidence', depth='quick')
        evaluation.tmp = base / 'calls'; evaluation.tmp.mkdir()
        return evaluation

    def test_real_gate_decode_cannot_read_encoder_only_artifact(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            base = Path(tmp); evaluation = self.evaluation(self.registered, base)
            stream = base / 'input'; stream.write_bytes(b'decoder-round-trip')
            stdout, _, measurement = evaluation.invoke('decode_stream', stream=stream)
            self.assertEqual(stdout.read_bytes(), stream.read_bytes())
            self.assertTrue(evaluation.raw['invocations'][-1]['decoder_bundle_only'])
            self.assertEqual(measurement['sandbox'], 'required')
            stream.write_bytes(b'need-encoder')
            stdout, _, measurement = evaluation.invoke('decode_stream', stream=stream, check=False)
            self.assertEqual(measurement['returncode'], 64)
            self.assertEqual(stdout.read_bytes(), b'')
            # The same compiled decoder succeeds with the larger runtime mounted:
            # failure above therefore comes from the gate's actual role isolation.
            result = runner.execute(['/candidate/decoder', '/candidate/decoder.dict', '/candidate/decoder.cfg'],
                output=base/'broad', readonly={'/candidate':self.registered/'runtime'}, stdin=stream,
                runtime_files=candidate.runtime_mounts(self.record['dependencies']), timeout=3)
            self.assertEqual(result['stdout'], 'need-encoder')

    def test_missing_charged_decoder_artifact_fails_execution_and_inventory(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            base = Path(tmp); root = base / self.record['candidate_digest']
            shutil.copytree(self.registered, root)
            evaluation = self.evaluation(root, base)
            (root/'decoder-runtime/decoder.dict').unlink()
            with self.assertRaises(Error):
                candidate.verify(root)
            stdout, _, measurement = evaluation.invoke('decode_stream', check=False)
            self.assertEqual(measurement['returncode'], 62)
            self.assertEqual(stdout.read_bytes(), b'')

    def test_cli_constant_is_in_charged_decoder_descriptor(self):
        original = candidate.costs(self.manifest, self.record)
        original_path = self.registered/'decoder-runtime'/candidate.DECODER_COMMAND_FILE
        constant = 'decoder-table-constant-' + 'Q'*1024
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            base = Path(tmp); source = base/'source'; shutil.copytree(self.source, source)
            manifest = copy.deepcopy(self.manifest)
            manifest['commands']['decode_stream'].append(constant)
            save(source/'candidate.json', manifest)
            work = base/'work'; work.mkdir()
            record = candidate.register(work, source)
            registered = work/'candidates'/record['candidate_digest']
            candidate.verify(registered)
            descriptor = registered/'decoder-runtime'/candidate.DECODER_COMMAND_FILE
            self.assertEqual(load(descriptor)['commands']['decode_stream'][-1], constant)
            added_bytes = descriptor.stat().st_size - original_path.stat().st_size
            self.assertGreaterEqual(added_bytes, len(constant.encode()))
            fixed = candidate.costs(manifest, record)
            self.assertEqual(fixed['decoder']['decoder_command_bytes'], descriptor.stat().st_size)
            self.assertEqual(fixed['decoder']['required_decoder_artifact_bytes'] - original['decoder']['required_decoder_artifact_bytes'], added_bytes)
            for component in ('compiled_decoder_bytes', 'nonplatform_decoder_dependency_bytes'):
                self.assertEqual(fixed['decoder'][component], original['decoder'][component])
            before = accounting.decoder_costs(100, 73, original, 1000)['actual']
            after = accounting.decoder_costs(100, 73, fixed, 1000)['actual']
            self.assertEqual(after['deployment_total_bytes'] - before['deployment_total_bytes'], added_bytes)

    def test_missing_descriptor_fails_gate_without_manifest_command_fallback(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            base = Path(tmp); root = base/self.record['candidate_digest']
            shutil.copytree(self.registered, root)
            evaluation = self.evaluation(root, base)
            self.assertIn('decode_stream', evaluation.m['commands'])
            self.assertIn('decode_stream', load(root/'source/candidate.json')['commands'])
            (root/'decoder-runtime'/candidate.DECODER_COMMAND_FILE).unlink()
            with self.assertRaises(Error):
                candidate.verify(root)
            with self.assertRaises((Error, OSError)):
                evaluation.invoke('decode_stream', check=False)
            self.assertEqual(evaluation.raw['invocations'], [])

    def test_combined_executable_is_charged_whole_and_v1_still_registers(self):
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                base = Path(tmp); source = base/'src'; manifest = source_fixture(source, version, combined=True)
                work = base/'work'; work.mkdir(); record = candidate.register(work, source)
                registered = work/'candidates'/record['candidate_digest']
                candidate.verify(registered)
                fixed = candidate.costs(manifest, record)
                self.assertEqual(fixed['decoder']['compiled_decoder_bytes'], (registered/'runtime/codec').stat().st_size)
                self.assertTrue(fixed['decoder']['combined_executable'])
                self.assertEqual(fixed['decoder']['role_separation'], version == 2)
                self.assertEqual((registered/'decoder-runtime').exists(), version == 2)
                result = runner.execute(['/candidate/codec'], output=base/'out', readonly={'/candidate':registered/('decoder-runtime' if version == 2 else 'runtime')},
                    runtime_files=candidate.runtime_mounts(record.get('decoder_dependencies', record['dependencies'])), timeout=3)
                self.assertEqual(result['returncode'], 0)

    def test_separate_diagnostic_decoder_has_its_own_runtime_instrumentation(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            base = Path(tmp)
            build = candidate.build(self.source, self.manifest, base/'build', sanitizer=True)
            names = {row['soname'] for row in build['decoder_dependencies']['libraries']}
            self.assertTrue(any(name.startswith('libasan.so') for name in names), names)
            self.assertTrue(any(name.startswith('libubsan.so') for name in names), names)
            runtime = base/'decoder-runtime'
            candidate.make_runtime(self.source, base/'build', self.manifest, build, runtime, decoder=True)
            self.assertFalse((runtime/'encoder').exists())
            stream = base/'input'; stream.write_bytes(b'diagnostic-decoder')
            result = runner.execute(['/candidate/decoder', '/candidate/decoder.dict', '/candidate/decoder.cfg'],
                output=base/'out', readonly={'/candidate':runtime}, stdin=stream, sanitizer=True,
                runtime_files=candidate.runtime_mounts(build['decoder_dependencies'], True), timeout=3)
            self.assertEqual(result['stdout'], 'diagnostic-decoder')

    def test_encoder_ubsan_does_not_certify_decoder_without_ubsan_runtime(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['sanitizer_build_commands'][1].append('-fsanitize-undefined-trap-on-error')
        candidate.manifest(manifest)
        with tempfile.TemporaryDirectory(dir=self.root) as tmp, self.assertRaises(Error) as caught:
            candidate.build(self.source, manifest, Path(tmp)/'build', sanitizer=True)
        self.assertEqual(caught.exception.code, 'blocked_toolchain')


if __name__ == '__main__':
    unittest.main()
