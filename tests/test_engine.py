import tempfile,unittest,hashlib
from pathlib import Path
from compression_lab.util import save

def data(p):
 from compression_lab.dataset import example_card
 p.mkdir();rows=[]
 for i,b in enumerate([b'01 abc\r\n'*2000,bytes(range(256))*20]):
  (p/f'{i}.bin').write_bytes(b);rows.append({'alias':f'object-{i}','source_group':f'group-{i}','split':'train' if i==0 else 'development','canonical_bytes':len(b),'canonical_sha256':hashlib.sha256(b).hexdigest(),'path':f'{i}.bin'})
 save(p/'manifest.json',{'schema_version':1,'objects':rows});save(p/'card.json',example_card('test'));return p/'card.json'
class EngineTest(unittest.TestCase):
 def test_full_vertical_slice_and_export(self):
  from compression_lab.engine import Engine
  from compression_lab.baselines import create
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);e=Engine.init(p/'work',data(p/'data'));self.assertEqual(e.profile()['metrics']['objects'],2)
   create(p/'candidate','zstd');reg=e.register(p/'candidate');queued=e.evaluate(reg['candidate_digest'],'full');self.assertEqual(queued['status'],'queued')
   result=e.wait(queued['job_id'],60);self.assertTrue(result['metrics']['quality_passed'],result)
   rid=result['metrics']['result_id'];r=e.export(rid,p/'export.zip');self.assertTrue((p/'export.zip').is_file());self.assertEqual(r['status'],'exported')
   self.assertEqual(e.status()['metrics']['completed_evaluations'],1)
   self.assertTrue(e.artifact(rid,limit=100)['metrics']['truncated'])
if __name__=='__main__':unittest.main()
