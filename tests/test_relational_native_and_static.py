import tempfile
import unittest
from pathlib import Path

from compression_lab import baselines, candidate
from compression_lab.engine import Engine
from compression_lab.util import Error, load, save
from compression_lab.workloads import registry, relational
from compression_lab.workloads.fixtures import create_dataset, static_records
from compression_lab.workloads.session import Session


ECHO = r'''#include <iostream>
#include <string>
#include <cstdlib>
int main() {
  std::string s;
  while (std::getline(std::cin, s)) {
    if(s.size()>1048576) return 2;
    auto p=s.find("\"id\":"); if(p==std::string::npos) return 2;
    auto id=std::strtoull(s.c_str()+p+5,nullptr,10);
    std::cout << "{\"version\":1,\"id\":" << id << ",\"ok\":true,\"result\":{\"native\":true}}" << std::endl;
    if(s.find("\"close\"")!=std::string::npos) break;
  }
}
'''


class NativeAndStaticRelationalTests(unittest.TestCase):
    def test_all_relational_encoder_fixtures_are_valid_and_keep_long_names(self):
        fixtures = static_records()
        self.assertEqual(len(fixtures), 17)
        self.assertEqual(max(len(x.alias) for x in fixtures), 1048576)
        for f in fixtures:
            b = relational.unpack(f.payload)
            self.assertEqual(relational.pack(b.schema_bytes, b.tables), f.payload)

    def test_native_static_relational_shared_evaluation_and_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            card = create_dataset(root / 'data', 'static_relational')
            engine = Engine.init(root / 'workspace', card)
            baselines.create(root / 'source', 'zstd', 1)
            m = load(root / 'source' / 'candidate.json')
            m['input_domain'] = 'rlb1-lexical'
            save(root / 'source' / 'candidate.json', m)
            r = engine.register(root / 'source')
            result = engine.evaluate(r['candidate_digest'], depth='quick', wait=True, workload='static_relational')
            self.assertTrue(result['metrics']['quality_passed'], result)
            self.assertEqual(result['metrics']['workload'], 'static_relational')
            raw = engine.raw(result['metrics']['result_id'])
            self.assertEqual(raw['gates']['valid_unusual_inputs']['total_cases'], 17)
            self.assertEqual(raw['relational']['encoding_floor_bytes_per_second'], 100_000_000)
            self.assertIn('relational-profile.json', raw['evidence_files'])
            p = engine.root / 'results' / result['metrics']['result_id'] / 'relational-profile.json'
            p.chmod(0o600)
            p.write_bytes(p.read_bytes() + b'changed')
            with self.assertRaises(Error) as ex:
                engine.export(result['metrics']['result_id'], root / 'wrong.zip')
            self.assertEqual(ex.exception.code, 'stale_workload_evidence_digest')

    def test_native_mutable_recipe_and_real_sanitized_persistent_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            baselines.create(root / 'source', 'stored', 0)
            base = load(root / 'source' / 'candidate.json')
            (root / 'source' / 'candidate.json').unlink()
            (root / 'source' / 'codec.cpp').write_text(ECHO)
            recipe_keys = ('build_commands', 'sanitizer_build_commands', 'build_output_paths', 'executable', 'toolchain', 'dependencies')
            m = dict(schema_version=1, workload='mutable_store', protocol_version=1, candidate_id='native-protocol-probe',
                     language='c++', threads=1, deterministic=True, source_paths=base['source_paths'],
                     artifact_paths=[], runtime_paths=[], command=['{runtime}/codec'], durability_claim='process_crash_only',
                     native_build={k: base[k] for k in recipe_keys})
            save(root / 'source' / registry.MANIFEST, m)
            r = registry.register(root, root / 'source')
            again = registry.register(root, root / 'source')
            self.assertEqual(r['build_cache_key'], again['build_cache_key'])
            self.assertEqual(r['candidate_digest'], again['candidate_digest'])
            registered = root / 'candidates' / r['candidate_digest']
            with Session(registered, root / 'store') as process:
                self.assertTrue(process.request('hello', {})['result']['native'])
            nm = registry.native_manifest(m)
            build = candidate.build(registered / 'source', nm, root / 'asan-build', sanitizer=True)
            candidate.make_runtime(registered / 'source', root / 'asan-build', nm, build, root / 'asan-runtime')
            self.assertTrue(any(d['soname'].startswith('libasan.so') for d in build['dependencies']['libraries']))
            self.assertTrue(any(d['soname'].startswith('libubsan.so') for d in build['dependencies']['libraries']))
            # Exercise teardown repeatedly: /proc/fd access can disappear before
            # an exiting namespace task is reaped by its parent.
            for trial in range(10):
                with Session(registered, root / f'asan-store-{trial}', runtime_override=(root / 'asan-runtime', build['dependencies']), diagnostic=True) as process:
                    self.assertTrue(process.request('hello', {})['result']['native'])
                    self.assertTrue(process.request('stats', {})['result']['native'])
                self.assertEqual(process.summary()['returncode'], 0)
            warnings = process.summary()['stderr'].splitlines()
            self.assertTrue(all('WARNING: reading executable name failed with errno 2, some stack frames may not be symbolized' in x for x in warnings), warnings)
            # A real out-of-bounds native control must be rejected by runtime
            # diagnostics, not merely by the presence of sanitizer flags.
            unsafe_source = ECHO.replace('  std::string s;', '  volatile int* bad=new int[1]; bad[5]=123; delete[] bad; std::string s;')
            (root / 'source' / 'codec.cpp').write_text(unsafe_source)
            bad_reg = registry.register(root, root / 'source')
            bad_root = root / 'candidates' / bad_reg['candidate_digest']
            bad_build = candidate.build(bad_root / 'source', nm, root / 'bad-build', sanitizer=True)
            candidate.make_runtime(bad_root / 'source', root / 'bad-build', nm, bad_build, root / 'bad-runtime')
            with Session(bad_root, root / 'bad-store', runtime_override=(root / 'bad-runtime', bad_build['dependencies']), diagnostic=True) as bad_process:
                with self.assertRaises(Error) as detected:
                    bad_process.request('hello', {})
                self.assertEqual(detected.exception.code, 'mutable_sanitizer_failure')
            m['native_build']['sanitizer_build_commands'][0] = [x for x in m['native_build']['sanitizer_build_commands'][0] if not x.startswith('-fsanitize=')]
            with self.assertRaises(Error) as ex:
                registry.validate(m)
            self.assertEqual(ex.exception.code, 'missing_sanitizer_instrumentation')


if __name__ == '__main__':
    unittest.main()
