import json,tempfile,unittest
from pathlib import Path
from compression_lab.connections import connect,disconnect
from compression_lab.util import Error
class Connections(unittest.TestCase):
 def test_codex_comment_preservation_and_idempotence(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'.codex').mkdir();f=p/'.codex/config.toml';original='# keep exactly\nmodel = "existing"\n[mcp_servers.other]\ncommand = "true"\n';f.write_text(original)
   connect('codex',p,False);self.assertEqual(f.read_text(),original)
   connect('codex',p,True);a=f.read_text();connect('codex',p,True);self.assertEqual(f.read_text(),a)
   disconnect('codex',p,True);self.assertEqual(f.read_text(),original);disconnect('codex',p,True)
 def test_zcode_native_precedence_unrelated_and_modified_owned(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'.zcode').mkdir();(p/'.agents').mkdir();f=p/'.zcode/config.json';f.write_text(json.dumps({'theme':'dark','mcp':{'servers':{'other':{'command':'x'}}}}));fallback=p/'.agents/mcp.json';fallback.write_text('{"mcpServers":{"fallback":{"command":"y"}}}')
   connect('zcode',p,True);d=json.loads(f.read_text());self.assertEqual(d['theme'],'dark');self.assertIn('compression-lab',d['mcp']['servers']);self.assertNotIn('compression-lab',fallback.read_text())
   d['mcp']['servers']['compression-lab']['args'].append('changed');f.write_text(json.dumps(d))
   with self.assertRaises(Error):disconnect('zcode',p,True)
   self.assertIn('changed',f.read_text())
if __name__=='__main__':unittest.main()
