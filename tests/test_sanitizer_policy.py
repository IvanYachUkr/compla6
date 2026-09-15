import tempfile,unittest
from pathlib import Path
from compression_lab import baselines,candidate
from compression_lab.util import load,Error
class SanitizerPolicy(unittest.TestCase):
 def manifest(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  p=Path(self.temp.name)/'candidate';baselines.create(p,'stored',0)
  return load(p/'candidate.json')
 def test_omitted_instrumentation_cannot_pass_as_a_sanitizer_build(self):
  m=self.manifest();m['sanitizer_build_commands']=m['build_commands']
  with self.assertRaises(Error):candidate.manifest(m)
 def test_disabling_flags_and_response_files_are_rejected(self):
  for arg in ('-fno-sanitize=all','@unreviewed.rsp'):
   with self.subTest(arg=arg):
    m=self.manifest();m['sanitizer_build_commands'][0].append(arg)
    with self.assertRaises(Error):candidate.manifest(m)
 def test_declared_native_sanitizers_remain_supported(self):
  self.assertEqual(candidate.manifest(self.manifest())['language'],'c++')
