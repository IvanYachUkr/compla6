import os,pwd,tempfile,unittest
from pathlib import Path
from compression_lab.util import Error,secure_read
from test_engine import data
class Owner(unittest.TestCase):
 def test_actual_uid_denial_and_fail_closed_certification(self):
  from compression_lab import owner
  from compression_lab.engine import Engine,doctor
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);Engine.init(p/'public',data(p/'data'));root=p/'owner';root.mkdir(mode=0o700);(root/'private-card.json').write_text('NOT READ UNTIL PRIVATE_STARTED')
   agent=65534
   if os.geteuid()==agent:agent=1000
   owner.init(root,agent,root/'private-card.json',p/'public',True);proof=owner.boundary(root,False);self.assertNotEqual(proof['agent_uid'],proof['owner_uid'])
   if os.geteuid()==0:
    import subprocess
    p.chmod(0o755);canary=p/'public-canary';canary.write_text('public');canary.chmod(0o444)
    code='from pathlib import Path;import sys;assert Path('+repr(str(canary))+').read_text()=="public"\ntry:Path('+repr(str(root/'private-card.json'))+').read_bytes();sys.exit(2)\nexcept PermissionError:sys.exit(0)'
    check=subprocess.run(['/usr/bin/python3','-I','-S','-c',code],user=agent,group=pwd.getpwuid(agent).pw_gid,extra_groups=[],capture_output=True)
    self.assertEqual(check.returncode,0,check.stderr)

   self.assertFalse((root/'private-snapshot').exists())
   if os.geteuid()==0:self.assertEqual(doctor(True,root)['status'],'blocked')
 def test_secure_import_rejects_symlink(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'link').symlink_to('/etc/passwd')
   with self.assertRaises(OSError):secure_read(p,'link')
if __name__=='__main__':unittest.main()
