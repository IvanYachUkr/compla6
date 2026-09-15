import tempfile,unittest
from pathlib import Path
class Native(unittest.TestCase):
 def test_two_native_baselines(self):
  from compression_lab import baselines,candidate,runner,stream
  for family in ('stored','zstd'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as td:
    p=Path(td);(p/'work').mkdir();baselines.create(p/'src',family);r=candidate.register(p/'work',p/'src');root=p/'work/candidates'/r['candidate_digest'];candidate.verify(root)
    b=stream.pack_stream(stream.ORIGINAL_MAGIC,[('a',b''),('prefix-0001',b'001 abc\r\n'*1000),('z'*4096,bytes(range(256))*10)]);(p/'in').write_bytes(b)
    kw={'output':p/'out','readonly':{'/candidate':root/'runtime'},'runtime_files':candidate.runtime_mounts(r['dependencies'])}
    runner.execute(['/candidate/codec','encode-stream'],stdin=p/'in',stdout=p/'arc',**kw);runner.execute(['/candidate/codec','decode-stream'],stdin=p/'arc',stdout=p/'dec',**kw)
    self.assertEqual((p/'dec').read_bytes(),b)
    if family=='zstd':self.assertLess((p/'arc').stat().st_size,len(b))
    bad=bytearray((p/'arc').read_bytes());bad[-1]^=1;(p/'bad').write_bytes(bad);out=runner.execute(['/candidate/codec','decode-stream'],stdin=p/'bad',stdout=p/'badout',check=False,**kw)
    self.assertNotEqual(out['returncode'],0);self.assertEqual((p/'badout').stat().st_size,0)
if __name__=='__main__':unittest.main()
