"""Differential wire and algebraic transform tests; no mock native codecs."""
import json
import random
import shutil
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from compression_lab import baselines
from compression_lab.stream import pack_stream, unpack_stream, StreamRecord, ORIGINAL_MAGIC, ARCHIVE_MAGIC
from compression_lab.util import load

NATIVE = Path(baselines.__file__).parent/'data/native'

class NativeHelperTests(unittest.TestCase):
    def test_crc_incremental_matches_ieee_and_transform_inverses(self):
        self.assertTrue((NATIVE/'lab_crc.hpp').is_file(), 'reusable CRC helper must exist')
        self.assertTrue((NATIVE/'lab_transforms.hpp').is_file())
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cpp=root/'test.cpp'; exe=root/'test'
            cpp.write_text(r'''
#include "lab_crc.hpp"
#include "lab_transforms.hpp"
#include <iostream>
int main() {
  for (size_t n=0; n<1027; ++n) {
    std::vector<uint8_t> b(n);
    for(size_t i=0;i<n;i++) b[i]=uint8_t((i*71+n*13)^(i>>2));
    auto full=lab::crc32(b.data(),b.size());
    auto c=lab::crc_update(0xffffffffU,b.data(),n/2);
    c=~lab::crc_update(c,b.data()+n/2,n-n/2);
    if(c!=full) return 2;
    for(unsigned transform=0; transform<3; transform++) {
      auto f=lab::forward(b,transform);auto r=lab::inverse(f,transform);
      if(r!=b || f.size()!=b.size()) return 3;
    }
    std::cout << n << " " << full << "\n";
  }
}''')
            subprocess.run(['g++','-std=c++17','-O2','-fsanitize=address,undefined','-fno-omit-frame-pointer','-no-pie','-I',str(NATIVE),str(cpp),'-o',str(exe)],check=True,capture_output=True)
            output=subprocess.run([str(exe)],check=True,capture_output=True,text=True).stdout
            for line in output.splitlines():
                n, crc=map(int,line.split())
                data=bytes(((i*71+n*13)^(i>>2))&255 for i in range(n))
                self.assertEqual(crc,zlib.crc32(data))

    def test_templates_roundtrip_raw_fallback_and_versioned_envelope(self):
        self.assertIn('transform', __import__('inspect').signature(baselines.create).parameters)
        rng=random.Random(94)
        records=tuple(StreamRecord('obj-'+str(i),b) for i,b in enumerate(
            [b'',b'\x00\xff\r\n',bytes(range(256))*32,bytes([0,0,0,1])*7001,rng.randbytes(12345)]))
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for transform in ('identity','delta8','shuffle4'):
                source=baselines.create(root/transform,'zstd',1,transform=transform)
                manifest=load(source/'candidate.json')
                cmd=[a.replace('{source}',str(source)).replace('{build}',str(source)) for a in manifest['build_commands'][0]]
                subprocess.run(cmd,check=True,capture_output=True)
                exe=source/'codec'
                original=pack_stream(ORIGINAL_MAGIC, records)
                encoded=subprocess.run([str(exe),'encode-stream'],input=original,check=True,capture_output=True).stdout
                decoded=subprocess.run([str(exe),'decode-stream'],input=encoded,check=True,capture_output=True).stdout
                self.assertEqual(decoded,original)
                for r in unpack_stream(encoded,ARCHIVE_MAGIC):
                    self.assertEqual(r.payload[:4], b'CLB1' if transform=='identity' else b'CLT1')
                mutated=bytearray(encoded);mutated[-1]^=1
                failed=subprocess.run([str(exe),'decode-stream'],input=mutated,capture_output=True)
                self.assertNotEqual(failed.returncode,0)
                self.assertEqual(failed.stdout,b'')
