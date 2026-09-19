"""Real native qualification regressions on a tiny stored-codec fixture."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from compression_lab import native_strings as native, runner, strings
from compression_lab.util import Error, save, sha


ROOT = Path(__file__).resolve().parents[1]
ENCODER = '''#include "codec.h"
#include <cstring>
#include <climits>
extern "C" int64_t lab_encode(const uint8_t* p,size_t n,uint8_t* out,size_t cap) {
  // INSERT_DIAGNOSTIC_PROBE
  if(n>cap)return -1; std::memcpy(out,p,n); return n;
}
'''
DECODER = '''#include "codec.h"
#include <cstring>
struct State { const uint8_t* p; size_t n; };
extern "C" void* lab_open(const uint8_t* p,size_t n) { return new State{p,n}; }
extern "C" int64_t lab_decode(void* opaque,uint8_t* out,size_t cap) {
  auto* s=static_cast<State*>(opaque); if(s->n>cap)return -1;
  std::memcpy(out,s->p,s->n); return s->n;
}
extern "C" void lab_close(void* opaque) { delete static_cast<State*>(opaque); }
'''


def local_build(argv, *, output, readonly, **kwargs):
    # Only replace the privileged launcher. Compilers, ELF inspection, the native
    # driver, qualification and evidence checks remain real. Fixtures are trusted.
    mappings = {'/output': str(output), **{k: str(v) for k, v in readonly.items()}}
    args = []
    for arg in argv:
        for old, new in mappings.items():
            arg = arg.replace(old, new)
        args.append(arg)
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True)
    return dict(argv=args, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


@unittest.skipUnless(shutil.which('g++'), 'native compiler unavailable')
class NativeQualificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.root = self.base/'work'
        raw = self.base/'input'; raw.write_bytes(b'alpha\nbeta\n')
        native.init(self.root, 'fixture', [raw], row_framing='none', cpu=min(os.sched_getaffinity(0)))
        self.source = self.root/'workbench'
        (self.source/'encoder.cpp').write_text(ENCODER)
        (self.source/'decoder.cpp').write_text(DECODER)
        (self.source/'ALGORITHM.md').write_text('Store and reconstruct every supplied byte. Test fixture.\n')
        manifest = native.template('stored-fixture')
        manifest.update(native.record_hypothesis(self.root, 'Store bytes', 'Exact reconstruction', 'Any mismatch'))
        self.manifest = self.source/'manifest.json'; save(self.manifest, manifest)
        spec = importlib.util.spec_from_file_location('qualification_launcher', ROOT/'benchmarks/dbtext/run.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        for context in (patch.object(runner, 'execute', local_build),
                        patch.object(strings, '_run', module.native_run),
                        patch.object(runner, 'HOST_LOCK', self.base/'fixture.lock')):
            context.start(); self.addCleanup(context.stop)

    def qualify(self):
        return native.qualify(self.root, self.manifest)

    def test_full_qualification_rejects_real_signed_overflow(self):
        (self.source/'encoder.cpp').write_text(ENCODER.replace('// INSERT_DIAGNOSTIC_PROBE',
            'volatile int a=INT_MAX,b=1; volatile int overflow=a+b; (void)overflow;'))
        with self.assertRaises(Error) as caught:
            self.qualify()
        self.assertEqual(caught.exception.code, 'native_valid_data_diagnostic_failed')
        self.assertIn('signed integer overflow', str(caught.exception))

    def test_valid_codec_qualifies_and_export_reuse_preserves_bytes(self):
        result = self.qualify()
        self.assertTrue(result['eligible']); self.assertEqual(result['roundtrips'], 8)
        bundle = native.export(self.root, result['result_id'])
        self.assertEqual(native.export(self.root, result['result_id']), bundle)
        with zipfile.ZipFile(bundle['path']) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read('measured/archives/'+sha(self.base/'input')+'.bin'), b'alpha\nbeta\n')

    def test_export_rejects_truncation_missing_members_and_changed_payload(self):
        result = self.qualify(); rid = result['result_id']
        path = Path(native.export(self.root, rid)['path'])
        original = path.read_bytes()
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        for damage in ('truncated', 'missing', 'changed'):
            with self.subTest(damage=damage):
                path.chmod(0o600)
                if damage == 'truncated':
                    path.write_bytes(b'PK\x03\x04')
                else:
                    with zipfile.ZipFile(path, 'w') as archive:
                        for name, data in files.items():
                            if name == 'measured/decoder.so':
                                if damage == 'missing': continue
                                data = b'x' * len(data)
                            archive.writestr(name, data)
                with self.assertRaises(Error) as caught:
                    native.export(self.root, rid)
                self.assertEqual(caught.exception.code, 'native_export_changed')
                path.write_bytes(original)

    def test_interrupted_export_leaves_no_final_archive_and_can_retry(self):
        result = self.qualify(); rid = result['result_id']
        final = self.root/'exports'/(rid+'.zip')
        with patch.object(zipfile.ZipFile, 'write', side_effect=OSError('interrupted write')):
            with self.assertRaisesRegex(OSError, 'interrupted write'):
                native.export(self.root, rid)
        self.assertFalse(final.exists())
        self.assertTrue(zipfile.is_zipfile(native.export(self.root, rid)['path']))
