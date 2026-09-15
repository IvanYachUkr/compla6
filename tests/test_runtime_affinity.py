import os,unittest
from compression_lab import runner
from compression_lab.util import Error
class RuntimeAffinity(unittest.TestCase):
 def test_narrowing_to_same_selected_cpu_is_not_runtime_drift(self):
  if not hasattr(os,'sched_getaffinity'):self.skipTest('Linux affinity unavailable')
  original=os.sched_getaffinity(0);before=runner.fingerprint()
  try:
   os.sched_setaffinity(0,{min(original)})
   self.assertEqual(runner.fingerprint()['runtime_digest'],before['runtime_digest'])
   runner.require_fingerprint(before)
  finally:os.sched_setaffinity(0,original)
 def test_forged_fingerprint_metadata_is_rejected(self):
  f=runner.fingerprint();f['cpu_model']='invented'
  with self.assertRaises(Error):runner.require_fingerprint(f)
if __name__=='__main__':unittest.main()
