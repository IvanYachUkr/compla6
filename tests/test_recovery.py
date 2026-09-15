import tempfile,time,unittest
from pathlib import Path
from compression_lab.engine import Engine
from compression_lab.util import save,load
from test_engine import data
class Recovery(unittest.TestCase):
 def test_committed_result_survives_missing_terminal_job_snapshot(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);e=Engine.init(p/'work',data(p/'data'))
   j={'schema_version':1,'run_id':e.state()['run_id'],'job_id':'j-recovery','candidate_digest':'a'*64,'depth':'full','track':'agent','status':'running','created_epoch':time.time()-100,'pid':99999999,'proc_start':'0'}
   def reserve(s):s['active_job']=j['job_id'];s['budgets'].setdefault('agent',{'evaluations':0,'wall_seconds':0})['evaluations']=1;return s
   e.ledger.update('test_reservation',reserve);save(e.root/'jobs'/j['job_id']/'job.json',j)
   rid=e._finish_job(dict(j),{'status':'failed','reason_codes':['timeout'],'wall_seconds':1})
   save(e.root/'jobs'/j['job_id']/'job.json',j) # simulate crash after journal commit, before terminal snapshot
   result=e.status(job=j['job_id'])
   self.assertEqual(result['metrics']['result_id'],rid)
   self.assertEqual(e.state()['results'],[rid]);self.assertEqual(e.state()['budgets']['agent']['wall_seconds'],1)
 def test_registration_budget_exhaustion_blocks_build(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);e=Engine.init(p/'work',data(p/'data'))
   def exhaust(s):s['budgets'].setdefault('agent',{'evaluations':0,'wall_seconds':0})['evaluations']=s['search_budget']['candidate_evaluations'];return s
   e.ledger.update('test_budget',exhaust)
   from compression_lab.util import Error
   with self.assertRaises(Error) as ex:e.register(p/'does-not-exist')
   self.assertEqual(ex.exception.code,'budget_exhausted')
if __name__=='__main__':unittest.main()
