import tempfile,unittest,hashlib
from pathlib import Path
class Gates(unittest.TestCase):
 def test_native_full_gate_suite(self):
  from compression_lab import candidate,baselines,dataset,gates
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'work').mkdir();baselines.create(p/'src','zstd');reg=candidate.register(p/'work',p/'src');rows=[]
   for i,b in enumerate([b'001\r\n'*10000,bytes(range(256))*100]):
    q=p/str(i);q.write_bytes(b);rows.append({'alias':f'object-{i}','group':f'group-{i}','split':'train' if i==0 else 'development','canonical_bytes':len(b),'source':q})
   r=gates.Evaluation(p/'work/candidates'/reg['candidate_digest'],rows,dataset.example_card('synthetic'),p/'result').run()
   self.assertTrue(r['quality_passed'],r.get('error'))
   self.assertEqual(len(r['timing_trials']['encode']),7);self.assertEqual(len(r['timing_trials']['decode']),7)
   self.assertFalse(r['eligible']);self.assertIn('smoke_not_certified',r['reason_codes'])
if __name__=='__main__':unittest.main()
