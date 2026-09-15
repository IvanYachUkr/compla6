import tempfile,unittest,json,copy,math,os,time,signal,subprocess,sys
from pathlib import Path
from compression_lab.util import Error,load,save,Ledger
from test_engine import data
class Contracts(unittest.TestCase):
 def test_reject_group_leak_and_nonfinite_json(self):
  from compression_lab import dataset
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);card=data(p/'data');m=load(card.parent/'manifest.json');m['objects'][1]['source_group']=m['objects'][0]['source_group'];save(card.parent/'manifest.json',m)
   with self.assertRaises(Error):dataset.read_source(card)
   bad=p/'nan.json';bad.write_text('{"value":NaN}')
   with self.assertRaises(Error):load(bad)
 def test_accounting_algebra_and_encoding_only_floor(self):
  from compression_lab import accounting,stream
  fixed={'fixed_bytes':11,'packed_source_bytes':13,'config_bytes':17,'binary_bytes':19,'nonplatform_dependency_bytes':23}
  r=accounting.costs(100,30,fixed,1000)
  self.assertEqual(r['actual']['historical_primary_bytes'],54);self.assertEqual(r['actual']['deployment_total_bytes'],100)
  self.assertEqual(r['projection']['deployment_total_bytes'],370)
  records=[('a',b'123'),('b-0',b'456789')];wire=stream.pack_stream(stream.ARCHIVE_MAGIC,records)
  self.assertEqual(len(wire),9+sum(12+len(n)+len(b) for n,b in records))
  trials=[{'elapsed_ns':1000000000,'canonical_bytes':100000000,'peak_rss_bytes':3} for _ in range(7)]
  self.assertEqual(accounting.timing(trials,'encode')['median_decimal_MB_per_second'],100)
  with self.assertRaises(Error):accounting.timing(trials[:6],'encode')
 def test_untrusted_dependency_path_rejected_before_read(self):
  from compression_lab import candidate,baselines
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);baselines.create(p/'src','zstd');m=load(p/'src/candidate.json');m['dependencies'][0]['build_path']='/private-do-not-open/secret'
   with self.assertRaises(Error) as ex:candidate.check_tools(m)
   self.assertEqual(ex.exception.code,'untrusted_dependency_path')
 def test_global_host_lock_and_terminal_latch(self):
  from compression_lab.util import lock
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);l=Ledger(p/'state');l.create({'stage':'created'});l.transition('public_search');l.transition('public_ready');l.transition('frozen');l.transition('private_started');l.transition('failed')
   with self.assertRaises(Error):l.transition('public_search')
   with lock(p/'shared'):
    with self.assertRaises(Error):
     with lock(p/'shared',nonblocking=True):pass
 def test_parent_death_terminates_isolated_child(self):
  # An actual SIGKILL of the evaluator must not leave a timed native child running.
  from compression_lab import candidate,baselines,runner
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'work').mkdir();baselines.create(p/'src','stored');f=p/'src/codec.cpp';f.write_text(f.read_text().replace('int main(int argc,char**argv){try{','int main(int argc,char**argv){try{for(;;){}'));r=candidate.register(p/'work',p/'src');root=p/'work/candidates'/r['candidate_digest'];script=p/'driver.py'
   script.write_text('import sys\nsys.path.insert(0,'+repr(str(Path(runner.__file__).parent.parent))+')\nfrom compression_lab import runner,candidate\nr='+repr(r['dependencies'])+'\nrunner.execute(["/candidate/codec","encode-stream"],output='+repr(str(p/'out'))+',readonly={"/candidate":'+repr(str(root/'runtime'))+'},runtime_files=candidate.runtime_mounts(r),timeout=20)\n')
   proc=subprocess.Popen([sys.executable,'-I','-S',str(script)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   descendants=[]
   for _ in range(100):
    time.sleep(.02);found=list(runner.process_tree(proc.pid)-{proc.pid})
    if len(found)>=2:descendants=found;break
   try:self.assertGreaterEqual(len(descendants),2)
   finally:
    proc.kill();proc.wait()
   time.sleep(.4)
   for pid in descendants:
    try:state=Path(f'/proc/{pid}/stat').read_text().split()[2]
    except FileNotFoundError:continue
    self.assertEqual(state,'Z',f'Live leaked process {pid}: {state}')
if __name__=='__main__':unittest.main()
