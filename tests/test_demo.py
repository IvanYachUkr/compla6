import tempfile,unittest
from pathlib import Path
from compression_lab import demo,dataset
class DemoInputs(unittest.TestCase):
 def test_real_bundled_inputs_and_group_normalization(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)
   for family,count in [('hadoop',6),('openstack',6),('bytes',8)]:
    c,rows=dataset.read_source(demo.inputs(p,family))
    self.assertEqual(len(rows),count)
    self.assertEqual(len({r['group'] for r in rows}),count)
    self.assertEqual({r['split'] for r in rows},{'train','development'})
    self.assertEqual(c['timing_policy']['role'],'smoke')
if __name__=='__main__':unittest.main()
