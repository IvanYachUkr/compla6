"""Direct stable-rustc checked builds; explicitly no ASan/UBSan claim."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

from compression_lab import candidate, runner
from compression_lab.util import Error, save, sha


SOURCE = r'''
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(String::as_str).unwrap_or("normal");
    match mode {
        "assert" => {
            debug_assert!(args.len() == 0, "checked-debug-assertion-canary");
            println!("assert-disabled");
        },
        "overflow" => {
            let value: u8 = args[2].parse().unwrap();
            println!("{}", value + 1);
        },
        _ => println!("normal-rust-canary"),
    }
}
'''


def manifest_fixture(source, identity):
    source.mkdir(); (source/'canary.rs').write_text(SOURCE)
    common = ['rustc', '--edition=2021', '-Ccodegen-units=1', '-Clinker=/usr/bin/gcc',
              '{source}/canary.rs', '-o', '{build}/codec']
    manifest = {'schema_version': 2, 'candidate_id': 'rust-checked-canary',
                'language': 'rust', 'input_domain': 'opaque_bytes', 'deterministic': True,
                'threads': 1, 'source_paths': ['canary.rs'], 'artifact_paths': [], 'runtime_paths': [],
                'build_commands': [[*common, '-Copt-level=2', '-Cdebug-assertions=no', '-Coverflow-checks=no']],
                'sanitizer_build_commands': [[*common, '-Copt-level=1', '-Cdebug-assertions=yes', '-Coverflow-checks=yes']],
                'build_output_paths': ['codec'], 'executable': 'codec',
                'commands': {operation: ['{runtime}/codec'] for operation in ('encode_stream', 'decode_stream', 'encode_dir', 'decode_dir')},
                'toolchain': {'rustc': identity}, 'dependencies': [],
                'decoder': {'executable': 'codec', 'build_output_paths': ['codec'], 'artifact_paths': [], 'runtime_paths': []},
                'diagnostics': {'kind': 'rust-checked-v1'}}
    save(source/'candidate.json', manifest)
    return candidate.manifest(manifest)


@unittest.skipUnless(os.environ.get('COMPRESSION_LAB_RUST_TOOLCHAIN'),
                     'Set evaluator-owned COMPRESSION_LAB_RUST_TOOLCHAIN for real Rust canaries')
class RustCheckedV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='clab-rust-v2-tests-')
        cls.root = Path(cls.temp.name); cls.identity = candidate.rust_identity()
        cls.source = cls.root/'source'; cls.manifest = manifest_fixture(cls.source, cls.identity)
        cls.builds = {}
        for name, checked in [('normal', False), ('checked', True)]:
            record = candidate.build(cls.source, cls.manifest, cls.root/(name+'-build'), sanitizer=checked)
            candidate.make_runtime(cls.source, cls.root/(name+'-build'), cls.manifest, record, cls.root/(name+'-runtime'), decoder=True)
            cls.builds[name] = record

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def call(self, build, *arguments, check=True):
        return runner.execute(['/candidate/codec', *arguments], output=self.root/'out',
            readonly={'/candidate':self.root/(build+'-runtime')},
            runtime_files=candidate.runtime_mounts(self.builds[build]['decoder_dependencies']),
            timeout=3, check=check)

    def test_checked_build_executes_normally_without_sanitizer_runtime(self):
        for name in ('normal', 'checked'):
            with self.subTest(build=name):
                record = self.builds[name]
                self.assertEqual(record['diagnostic_kind'], 'rust-checked-v1')
                libraries = [row['soname'] for row in record['decoder_dependencies']['libraries']]
                self.assertFalse(any(library.startswith(('libasan.so', 'libubsan.so')) for library in libraries))
                result = self.call(name)
                self.assertEqual(result['stdout'], 'normal-rust-canary\n')
                self.assertEqual(result['sandbox'], 'required')
                self.assertEqual(result['resources']['rlimit_as_bytes'], 2*1024**3)

    def test_checked_build_really_panics_on_debug_assertion_and_overflow(self):
        for arguments, release_output, panic_fragment in [
            (('assert',), 'assert-disabled\n', 'checked-debug-assertion-canary'),
            (('overflow', '255'), '0\n', 'attempt to add with overflow'),
        ]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.call('normal', *arguments)['stdout'], release_output)
                checked = self.call('checked', *arguments, check=False)
                self.assertNotEqual(checked['returncode'], 0)
                self.assertIn('panicked at', checked['stderr'])
                self.assertIn(panic_fragment, checked['stderr'])
                self.assertEqual(checked['stdout'], '')

    def test_compiler_identity_includes_actual_versioned_llvm_payload(self):
        toolchain = candidate.rust_toolchain()
        inventory = {row['path']:row for row in self.identity['sysroot_files']}
        binaries = []
        for path in (toolchain/'lib').glob('*LLVM*'):
            if path.is_file():
                with path.open('rb') as source:
                    if source.read(4) == b'\x7fELF':
                        binaries.append(path)
        self.assertTrue(binaries, 'The configured Rust compiler must expose its real LLVM library')
        for binary in binaries:
            relative = binary.relative_to(toolchain).as_posix()
            self.assertIn(relative, inventory)
            self.assertEqual(inventory[relative]['sha256'], sha(binary))

    def test_sysroot_metadata_and_native_target_linkers_are_pinned(self):
        toolchain = candidate.rust_toolchain()
        inventory = {row['path'] for row in self.identity['sysroot_files']}
        target = toolchain/'lib/rustlib/x86_64-unknown-linux-gnu'
        files = [p for p in target.rglob('*') if p.is_file() and
                 (p.suffix in ('.rmeta', '.o') or (target/'bin') in p.parents)]
        self.assertTrue(files)
        for file in files:
            self.assertIn(file.relative_to(toolchain).as_posix(), inventory)

    def test_conflicting_checked_codegen_spellings_are_rejected(self):
        for flags in [
            ['-Cdebug-assertions=no'], ['-C', 'overflow-checks=no'],
            ['--codegen', 'debug-assertions=no'], ['--codegen=overflow-checks=no'],
            ['-Cdebug_assertions=no'], ['-Coverflow_checks=no'],
        ]:
            with self.subTest(flags=flags):
                manifest = copy.deepcopy(self.manifest)
                manifest['sanitizer_build_commands'][0] += flags
                with self.assertRaises(Error):
                    candidate.manifest(manifest)

    def test_native_execution_cannot_mount_the_build_toolchain(self):
        with self.assertRaises(Error) as caught:
            runner.execute(['/bin/true'], output=self.root/'forbidden',
                           readonly={'/toolchain':candidate.rust_toolchain()})
        self.assertEqual(caught.exception.code, 'invalid_mount')


if __name__ == '__main__':
    unittest.main()
