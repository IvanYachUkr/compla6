import tempfile,unittest,json,os,time,signal
from pathlib import Path
from compression_lab.util import Error,load,save
from test_engine import data
class Negative(unittest.TestCase):
 def test_missing_artifact_symlink_and_stale_snapshot(self):
  from compression_lab import candidate,baselines
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'work').mkdir();baselines.create(p/'src','stored');m=load(p/'src/candidate.json');m['artifact_paths']=['missing.dict'];m['training']={'kind':'data-independent'};save(p/'src/candidate.json',m)
   with self.assertRaises(Error):candidate.register(p/'work',p/'src')
   m['artifact_paths']=[];save(p/'src/candidate.json',m);(p/'src/leak').symlink_to('/etc/passwd')
   with self.assertRaises(Error):candidate.register(p/'work',p/'src')
   (p/'src/leak').unlink();r=candidate.register(p/'work',p/'src');root=p/'work/candidates'/r['candidate_digest']
   (p/'src/codec.cpp').write_text('changed mutable workbench');candidate.verify(root)
   target=root/'runtime/codec';target.chmod(0o644);b=bytearray(target.read_bytes());b[-1]^=1;target.write_bytes(b)
   with self.assertRaises(Error):candidate.verify(root)
 def test_numeric_normalization_is_rejected(self):
  from compression_lab import candidate,baselines,dataset,gates
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'work').mkdir();baselines.create(p/'src','stored');f=p/'src/codec.cpp';f.write_text(f.read_text().replace('B encode(const B&b,const B&dict){',"B encode(const B&original,const B&dict){B b=original;for(auto &x:b)if(x=='0')x='1';"))
   r=candidate.register(p/'work',p/'src');card,rows=dataset.read_source(data(p/'data'));result=gates.Evaluation(p/'work/candidates'/r['candidate_digest'],rows,card,p/'result','quick').run()
   self.assertFalse(result['quality_passed']);self.assertIn('byte_mismatch',result['reason_codes'])
 def test_alias_heap_read_rejected_by_sanitizer_gate(self):
  from compression_lab import candidate,baselines,dataset,gates
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'work').mkdir();baselines.create(p/'src','stored');f=p/'src/codec.cpp';f.write_text(f.read_text().replace('bool alias(const std::string&s){','__attribute__((no_sanitize("undefined"))) bool alias(const std::string&s){char*tmp=(char*)malloc(s.size());memcpy(tmp,s.data(),s.size());volatile unsigned char overread=tmp[s.size()];(void)overread;free(tmp);'))
   r=candidate.register(p/'work',p/'src');card,rows=dataset.read_source(data(p/'data'));result=gates.Evaluation(p/'work/candidates'/r['candidate_digest'],rows,card,p/'result','full').run()
   self.assertFalse(result['quality_passed']);self.assertTrue(any('AddressSanitizer' in x.get('stderr','') for x in result['invocations']),result)
 def test_cancel_durable_job_and_keep_attempt(self):
  from compression_lab.engine import Engine
  from compression_lab.baselines import create
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);e=Engine.init(p/'work',data(p/'data'));create(p/'src','stored');f=p/'src/codec.cpp';f.write_text(f.read_text().replace('int main(int argc,char**argv){try{','int main(int argc,char**argv){try{for(;;){}'))
   r=e.register(p/'src');j=e.evaluate(r['candidate_digest'],'quick');e.cancel(j['job_id']);done=e.wait(j['job_id'],15)
   self.assertEqual(done['status'],'cancelled',done);self.assertEqual(e.state()['budgets']['agent']['evaluations'],1);self.assertIsNone(e.state()['active_job'])
 def test_timeout_kills_actual_native_process(self):
  from compression_lab import candidate,baselines,runner
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'work').mkdir();baselines.create(p/'src','stored');f=p/'src/codec.cpp';f.write_text(f.read_text().replace('int main(int argc,char**argv){try{','int main(int argc,char**argv){try{for(;;){}'));r=candidate.register(p/'work',p/'src');root=p/'work/candidates'/r['candidate_digest']
   with self.assertRaises(Error) as caught:runner.execute(['/candidate/codec','encode-stream'],output=p/'out',readonly={'/candidate':root/'runtime'},runtime_files=candidate.runtime_mounts(r['dependencies']),timeout=.15)
   self.assertEqual(caught.exception.code,'timeout')
if __name__=='__main__':unittest.main()
