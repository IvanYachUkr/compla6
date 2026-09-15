"""Full certification and an exported native decoder, not just helper-unit tests."""
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from compression_lab import baselines, candidate, runner
from compression_lab.engine import Engine
from compression_lab.stream import pack_stream, ORIGINAL_MAGIC
from compression_lab.util import load
from test_engine import data

class FullRedesignTests(unittest.TestCase):
    def test_transform_and_static_asset_full_gates_and_exported_independent_decode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); e=Engine.init(root/'work',data(root/'data'))
            for name,transform,link in [('transformed','shuffle4','shared'),('embedded','identity','static-codec')]:
                source=baselines.create(root/name,'zstd',1,transform=transform,link_mode=link)
                registered=e.register(source)
                result=e.evaluate(registered['candidate_digest'],'full',wait=True)
                self.assertTrue(result['metrics']['quality_passed'],result)
                raw=e.raw(result['metrics']['result_id'])
                self.assertFalse(raw['eligible'])  # smoke remains non-certifiable
                self.assertEqual(len(raw['timing_trials']['encode']),7)
                self.assertEqual(len(raw['timing_trials']['decode']),7)
                self.assertEqual(raw['deployment_views']['standalone'],raw['accounting'])
                if link=='static-codec':
                    self.assertEqual(raw['fixed_costs']['nonplatform_dependency_bytes'],0)
                    self.assertGreater(raw['fixed_costs']['raw_source_config_bytes'],500000)
                    self.assertGreater(raw['fixed_costs']['binary_bytes'],100000)
                    self.assertTrue((source/'vendor/libzstd.a').is_file())
                exported=root/(name+'.zip'); e.export(result['metrics']['result_id'],exported)
                fresh=root/(name+'-alien-root'); fresh.mkdir()
                with zipfile.ZipFile(exported) as z:z.extractall(fresh)
                runtime=fresh/'candidate/runtime'; (runtime/'codec').chmod(0o555)
                _,reg=candidate.verify(e.root/'candidates'/registered['candidate_digest'])
                original=pack_stream(ORIGINAL_MAGIC,[('alien-input',bytes(range(256))*55+b'\r\n\x00\xffodd')])
                inp=root/(name+'.hbi'); inp.write_bytes(original)
                archive=root/(name+'.hba'); decoded=root/(name+'-decoded.hbi')
                # Only the MOVED runtime and platform closure are visible to each codec.
                # Original data, candidate source, engine and provider access are not mounted.
                mounts=candidate.runtime_mounts(reg['dependencies'])
                for op,stdin,stdout in [('encode-stream',inp,archive),('decode-stream',archive,decoded)]:
                    runner.execute(['/candidate/codec',op],output=root/(name+'-'+op),
                        readonly={'/candidate':runtime},runtime_files=mounts,stdin=stdin,stdout=stdout,mode='required')
                self.assertEqual(decoded.read_bytes(),original)

    def test_identity_archives_are_wire_identical_to_unmodified_0_2_2_for_all_families(self):
        old=Path(__file__).resolve().parents[1]/'provenance/original-native/baseline.cpp'
        self.assertTrue(old.is_file())
        original=pack_stream(ORIGINAL_MAGIC,[('a',b''),('b',b'001.00\r\n\xff'*801),('c',bytes(range(256))*111)])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for family,level in [('stored',0),('zstd',1),('lz4',0),('brotli',4),('xz',3)]:
                source=baselines.create(root/family,family,level);m=load(source/'candidate.json')
                newcmd=[s.replace('{source}',str(source)).replace('{build}',str(source)) for s in m['build_commands'][0]]
                subprocess.run(newcmd,check=True,capture_output=True)
                oldexe=root/(family+'-old');oldcmd=[str(old) if s==str(source/'codec.cpp') else str(oldexe) if s==str(source/'codec') else s for s in newcmd]
                subprocess.run(oldcmd,check=True,capture_output=True)
                archives=[]
                for exe in (oldexe,source/'codec'):
                    archives.append(subprocess.run([str(exe),'encode-stream'],input=original,capture_output=True,check=True).stdout)
                self.assertEqual(archives[0],archives[1],family)
                for exe in (oldexe,source/'codec'):
                    decoded=subprocess.run([str(exe),'decode-stream'],input=archives[1],capture_output=True,check=True).stdout
                    self.assertEqual(decoded,original)
