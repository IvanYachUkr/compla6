import hashlib,json,tracemalloc,unittest
from compression_lab.gates import corruption_cases
from compression_lab.stream import StreamRecord


class CorruptionMemory(unittest.TestCase):
 def test_same_35_cases_in_the_original_order(self):
  rows=[{'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()}
        for value in corruption_cases([StreamRecord('object-0',bytes(range(256))+b'Z')])]
  self.assertEqual(len(rows),35)
  fingerprint=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
  self.assertEqual(fingerprint,'3f7382cbe0a62bfb02c8cb336d8f472a0c11e3255139b10dd10bc00e74d19d11')

 def test_mutations_do_not_accumulate_complete_archive_copies(self):
  payload=b'x'*(4*1024*1024)
  tracemalloc.start()
  try:
   cases=corruption_cases([StreamRecord('object-0',payload)])
   self.assertIs(iter(cases),cases)
   count=0
   for value in cases:count+=1
   _,peak=tracemalloc.get_traced_memory()
  finally:tracemalloc.stop()
  self.assertEqual(count,35)
  self.assertLess(peak,10*len(payload))


if __name__=='__main__':unittest.main()
