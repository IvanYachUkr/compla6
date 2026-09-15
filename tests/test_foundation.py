import tempfile,unittest
from pathlib import Path
from compression_lab.util import Ledger,Error
from compression_lab.hcb import encode_hcb,decode_hcb
from compression_lab.stream import pack_stream,unpack_stream,ORIGINAL_MAGIC
class Foundation(unittest.TestCase):
 def test_exact_formats(self):
  b=encode_hcb([('a\nx',b'\0\xff001\r\n')]);self.assertEqual(decode_hcb(b),(('a\nx',b'\0\xff001\r\n'),))
  with self.assertRaises(ValueError):decode_hcb(b+b'x')
  s=pack_stream(ORIGINAL_MAGIC,[('a',b),('z'*4096,b'')]);self.assertEqual(len(unpack_stream(s,ORIGINAL_MAGIC)),2)
  with self.assertRaises(ValueError):unpack_stream(s+b'x',ORIGINAL_MAGIC)
 def test_durable_terminal_state(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);l=Ledger(p);l.create({'stage':'created','run_id':'x'})
   for stage in ('public_search','public_ready','frozen','private_started','complete'):l.transition(stage)
   (p/'state.json').write_text('{}');self.assertEqual(l.read()['stage'],'complete')
   with self.assertRaises(Error):l.transition('public_search')
   with (p/'events.jsonl').open('ab') as f:f.write(b'{"interrupted":')
   self.assertEqual(l.read()['stage'],'complete')
if __name__=='__main__':unittest.main()
