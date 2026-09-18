import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from compression_lab import native_remote, native_strings, strings
from compression_lab.util import Error, digest, load, save, sha


ROOT = Path(__file__).resolve().parents[1]


class NativeFramingTests(unittest.TestCase):
    def test_nul_rows_preserve_lf_empty_rows_and_unterminated_tail(self):
        for raw, expected in [(b'', []), (b'\0', [b'\0']),
                              (b'a\nb\0\0tail', [b'a\nb\0', b'\0', b'tail'])]:
            self.assertEqual(strings.rows(raw, 'nul'), expected)
            self.assertEqual(b''.join(strings.rows(raw, 'nul')), raw)

    def test_new_framing_is_pinned_and_old_lf_config_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'input'; driver = root/'driver'
            source.write_bytes(b'a\nb\0\0tail'); driver.write_bytes(b'fixture driver')
            for framing, count in [('lf', 2), ('nul', 3), ('none', None)]:
                workspace = root/framing
                strings.init(workspace, [('input', source)], driver, {},
                             min(os.sched_getaffinity(0)), row_framing=framing)
                config = strings.config(workspace)
                self.assertEqual(config['row_framing'], framing)
                self.assertEqual(config['columns'][0]['rows'], count)
            legacy = load(root/'lf/strings.json'); legacy.pop('row_framing')
            save(root/'lf/strings.json', legacy)
            self.assertEqual(strings.config(root/'lf'), legacy)
            self.assertEqual(digest(strings.config(root/'lf')), digest(legacy))

    def test_bulk_bytes_reject_row_candidates_before_creating_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'input'; driver = root/'driver'
            source.write_bytes(b'a\0b\n'); driver.write_bytes(b'fixture driver')
            strings.init(root/'work', [('input', source)], driver, {},
                         min(os.sched_getaffinity(0)), row_framing='none')
            manifest = root/'candidate.json'
            save(manifest, dict(name='invalid', variant='rows', encoder='absent', decoder='absent'))
            with self.assertRaises(Error) as caught:
                strings.evaluate(root/'work', manifest)
            self.assertEqual(caught.exception.code, 'strings_rows_not_configured')
            self.assertEqual(list((root/'work/evidence').iterdir()), [])

    def test_remote_accepts_any_complete_configured_corpus_and_rejects_same_size_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'input'; driver = root/'driver'
            source.write_bytes(b'a\0b\0'); driver.write_bytes(b'fixture driver')
            strings.init(root/'work', [('sql-column', source)], driver, {},
                         min(os.sched_getaffinity(0)), row_framing='nul')
            config = strings.config(root/'work')
            save(root/'work/server.json', {'worker': 'owner-configured'})
            (root/'work/server-jobs').mkdir()
            row = dict(columns=[{k: v for k, v in config['columns'][0].items() if k != 'path'}],
                       original_bytes=4, workload_digest=digest(config), variant='rows')
            with patch.object(native_strings, 'result', return_value=(row, {})), \
                 patch.object(native_strings, 'export', return_value={'sha256': 'export-hash'}):
                state = native_remote.submit(root/'work', 'qualified-result')
                self.assertEqual(state['status'], 'queued')
                request = load(root/'work/server-jobs'/state['job_id']/'request.json')
                self.assertEqual(request['workload_digest'], digest(config))
                self.assertEqual(request['row_framing'], 'nul')
                row['columns'][0]['sha256'] = 'f'*64
                with self.assertRaises(Error) as caught:
                    native_remote.submit(root/'work', 'substituted-result')
                self.assertEqual(caught.exception.code, 'server_requires_complete_workload')

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'Official MCP SDK unavailable')
    def test_bulk_only_commission_can_finish_with_its_qualified_bulk_result(self):
        import asyncio
        from compression_lab.native_strings_server import make_server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'input'; driver = root/'driver'
            source.write_bytes(b'data'); driver.write_bytes(b'fixture driver')
            strings.init(root/'work', [('input', source)], driver, {},
                         min(os.sched_getaffinity(0)), row_framing='none')
            save(root/'work/commission.json', {'contract':'native-exact-dataset-v1', 'variants':['bulk']})
            (root/'work/workbench/report.md').write_text('Measured tradeoff; source review remains pending.')
            server = make_server(root/'work', root/'work/workbench', 8000, 't'*32)
            with patch.object(native_strings, 'result', return_value=({'variant':'bulk'}, {})), \
                 patch.object(native_strings, 'export', return_value={'result_id':'qualified-bulk'}):
                asyncio.run(server.call_tool('finish', {'bulk_result':'qualified-bulk', 'rows_result':None,
                                                       'report_path':'report.md'}))
            receipt = load(root/'work/FINISH.json')
            self.assertTrue(receipt['accepted'])
            self.assertEqual(receipt['native_exports'], [{'result_id':'qualified-bulk'}])


@unittest.skipUnless(shutil.which('g++') and shutil.which('gcc'), 'native compilers unavailable')
class NativeRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.build = Path(cls.temp.name)
        cls.driver = cls.build/'driver'
        subprocess.run(['g++', '-O2', ROOT/'src/compression_lab/data/strings/driver.cpp',
                        '-ldl', '-o', cls.driver], check=True, capture_output=True)
        # An independent stored-codec fixture. NUL is the declared row boundary;
        # newlines inside those rows must survive every selected-row check.
        source = cls.build/'codec.c'
        source.write_text('''#include <stdint.h>
#include <stdlib.h>
#include <string.h>
struct State { const uint8_t *p; size_t n; };
#ifndef DECODE_ONLY
int64_t lab_encode(const uint8_t *p, size_t n, uint8_t *out, size_t cap) {
    if (n > cap) return -1; memcpy(out,p,n); return n;
}
#else
void *lab_open(const uint8_t *p, size_t n) {
    struct State *s=malloc(sizeof(*s)); if(s){s->p=p;s->n=n;} return s;
}
int64_t lab_decode(void *state, uint8_t *out, size_t cap) {
    struct State *s=state; if(s->n>cap)return -1;
    memcpy(out,s->p,s->n); return s->n;
}
int64_t lab_rows(void *state, const uint64_t *ids, size_t count,
                 uint8_t *out, size_t cap, uint64_t *offsets) {
    struct State *s=state; size_t row=0, start=0, chosen=0, pos=0; offsets[0]=0;
    for(size_t end=0;end<s->n;++end) if(s->p[end]==0 || end+1==s->n) {
        size_t n=end+1-start;
        if(chosen<count && ids[chosen]==row){
            if(n>cap-pos)return -1; memcpy(out+pos,s->p+start,n);
            pos+=n; offsets[++chosen]=pos;
        }
        ++row; start=end+1;
    }
    return chosen==count ? (int64_t)pos : -1;
}
void lab_close(void *state) { free(state); }
#endif
''')
        for role in ('encoder', 'decoder'):
            subprocess.run(['gcc', '-O2', '-shared', '-fPIC',
                            *(['-DDECODE_ONLY'] if role == 'decoder' else []),
                            source, '-o', cls.build/(role+'.so')], check=True, capture_output=True)
        spec = importlib.util.spec_from_file_location('dbtext_run', ROOT/'benchmarks/dbtext/run.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        cls.launch = staticmethod(module.native_run)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_nul_full_evaluation_keeps_eight_passes_separate_from_bulk_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); raw = root/'input'; raw.write_bytes(b'one\ninside\0\0last')
            for framing, variant in [('nul', 'rows'), ('none', 'bulk')]:
                workspace = root/framing
                strings.init(workspace, [('input', raw)], self.driver, {},
                             min(os.sched_getaffinity(0)), row_framing=framing)
                manifest = self.build/'manifest.json'
                save(manifest, dict(name='stored', variant=variant, encoder='encoder.so', decoder='decoder.so'))
                with patch('compression_lab.runner.HOST_LOCK', root/'measurement.lock'):
                    result = strings.evaluate(workspace, manifest, run=self.launch)
                self.assertTrue(result['quality_passed'], result.get('error'))
                self.assertEqual(result['roundtrips'], 8)
                self.assertEqual(result['selective_checks'], 40 if variant == 'rows' else 0)
                self.assertEqual(result['accounting']['archive_bytes'], 16)
                self.assertEqual(result['accounting']['package_bytes'], 16+(self.build/'decoder.so').stat().st_size)
                if variant == 'rows':
                    self.assertEqual(result['columns'][0]['rows'], 3)
                    self.assertEqual(result['selective']['100']['rows'], 3)
                else:
                    self.assertEqual(result['selective'], {})
                    self.assertIsNone(result['columns'][0]['rows'])
                records = [json.loads(line) for line in (Path(result['evidence'])/'trials.jsonl').read_text().splitlines()]
                self.assertEqual([r['trial'] for r in records], list(range(8)))
                self.assertTrue(all(r['exact'] for r in records))

    def test_owner_init_builds_pinned_runtime_and_does_not_replace_a_workspace(self):
        from compression_lab.cli import dispatch, parser
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'bytes.bin'; source.write_bytes(b'a\0b\n')
            workspace = root/'work'
            args = parser().parse_args(['native-init', '--workspace', str(workspace),
                                        '--dataset', 'fixture', '--input', str(source),
                                        '--row-framing', 'none'])
            result = dispatch(args)
            self.assertEqual(result['status'], 'ok')
            config = strings.config(workspace)
            self.assertEqual(config['row_framing'], 'none')
            self.assertEqual(config['columns'][0]['sha256'], sha(source))
            self.assertEqual(load(workspace/'commission.json')['variants'], ['bulk'])
            self.assertEqual(load(workspace/'commission.json')['implementation'], 'from_scratch')
            self.assertTrue((workspace/'runtime/driver').is_file())
            self.assertTrue((workspace/'INSTRUCTIONS.md').stat().st_size)
            self.assertEqual(load(workspace/'workbench/manifest-template.json')['variant'], 'bulk')
            config_bytes = (workspace/'strings.json').read_bytes()
            with self.assertRaises(Error) as caught:
                dispatch(args)
            self.assertEqual(caught.exception.code, 'native_workspace_not_empty')
            self.assertEqual((workspace/'strings.json').read_bytes(), config_bytes)
            runtime_source = workspace/'runtime/driver.cpp'
            runtime_source.chmod(0o600); runtime_source.write_text('changed')
            with self.assertRaises(Error) as caught:
                strings.config(workspace)
            self.assertEqual(caught.exception.code, 'strings_driver_source_changed')

    def test_portable_build_can_select_bulk_only_and_rejects_changed_published_sources(self):
        spec = importlib.util.spec_from_file_location('dbtext_build', ROOT/'benchmarks/dbtext/build.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); here = root/'dbtext'; upstream = root/'upstream'
            shutil.copytree(ROOT/'benchmarks/dbtext', here)
            shutil.copytree(ROOT/'benchmarks/upstream/fsst', upstream/'fsst')
            shutil.copyfile(ROOT/'benchmarks/SOURCE_MAP.json', root/'SOURCE_MAP.json')
            with patch.object(module, 'HERE', here), patch.object(module, 'UPSTREAM', upstream):
                module.build(root/'build', methods=['lz4'])
                manifest = load(root/'build/lz4/manifest.json')
                self.assertEqual(manifest['variant'], 'bulk')
                self.assertFalse((root/'build/onpairplus').exists())
                self.assertTrue((root/'build/driver').is_file())
                data = root/'data'; data.mkdir(); (data/'custom.bin').write_bytes(b'a\nb\0\0last')
                columns = root/'columns.json'
                save(columns, [dict(name='custom.bin', bytes=9, sha256=sha(data/'custom.bin'))])
                run = subprocess.run([os.sys.executable, ROOT/'benchmarks/dbtext/run.py',
                    '--data-dir', data, '--columns', columns, '--build-dir', root/'build',
                    '--out', root/'result', '--methods', 'lz4', '--cpu', str(min(os.sched_getaffinity(0)))],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr)
                summary = load(root/'result/summary.json')
                self.assertTrue(summary['results'][0]['quality_passed'])
                self.assertEqual(summary['results'][0]['roundtrips'], 8)
                metadata = load(root/'result/run-metadata.json')
                self.assertEqual(metadata['original_bytes'], 9)
                self.assertFalse(metadata['smoke'])
                self.assertEqual(metadata['parameters']['row_framing'], 'lf')
                smoke = subprocess.run([os.sys.executable, ROOT/'benchmarks/dbtext/run.py',
                    '--data-dir', data, '--build-dir', root/'build', '--out', root/'smoke',
                    '--smoke', '--cpu', str(min(os.sched_getaffinity(0)))],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(smoke.returncode, 0, smoke.stderr)
                metadata = load(root/'smoke/run-metadata.json')
                self.assertTrue(metadata['smoke'])
                self.assertEqual(metadata['parameters']['trials'], 1)
                self.assertEqual(load(root/'smoke/strings.json')['trials'], 7)
                with (here/'reference.cpp').open('a') as stream:
                    stream.write('\n// unexpected source edit\n')
                with self.assertRaisesRegex(RuntimeError, 'Source checksum mismatch'):
                    module.build(root/'changed', methods=['lz4'])
                self.assertFalse((root/'changed/driver').exists())


if __name__ == '__main__':
    unittest.main()
