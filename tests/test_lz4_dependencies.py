import tempfile,unittest
from pathlib import Path
from compression_lab import baselines,candidate,runner
from compression_lab.stream import pack_stream,unpack_stream,ORIGINAL_MAGIC,ARCHIVE_MAGIC
class Lz4Closure(unittest.TestCase):
 def test_transitive_xxhash_is_shipped_hashed_licensed_and_charged(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'work').mkdir();baselines.create(p/'src','lz4',0)
   r=candidate.register(p/'work',p/'src');root=p/'work/candidates'/r['candidate_digest'];m,_=candidate.verify(root)
   for lib in r['dependencies']['libraries']:
    if lib['platform']:continue
    self.assertIn(lib['soname'],{x['soname'] for x in m['dependencies']})
    self.assertTrue((root/'runtime/lib'/lib['soname']).is_file())
   if 'libxxhash.so.0' in {x['soname'] for x in r['dependencies']['libraries']}:
    self.assertTrue((root/'runtime/licenses/libxxhash0.copyright').is_file())
   original=pack_stream(ORIGINAL_MAGIC,[('a',b'abc'*10000),('b',b'')]);(p/'input').write_bytes(original)
   options=dict(output=p/'out',readonly={'/candidate':root/'runtime'},runtime_files=candidate.runtime_mounts(r['dependencies']))
   runner.execute(['/candidate/codec','encode-stream'],stdin=p/'input',stdout=p/'encoded',**options)
   runner.execute(['/candidate/codec','decode-stream'],stdin=p/'encoded',stdout=p/'decoded',**options)
   self.assertEqual((p/'decoded').read_bytes(),original)
if __name__=='__main__':unittest.main()
