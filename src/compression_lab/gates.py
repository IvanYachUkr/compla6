"""One scoring pipeline for public and owner-only evaluation."""
from __future__ import annotations
import hashlib,json,random,shutil,tempfile,time
from itertools import islice
from pathlib import Path
from . import candidate,accounting,runner,deployment,dataset
from .util import Error,save,sha,load,digest
from .stream import StreamRecord,pack_stream,unpack_stream,ORIGINAL_MAGIC,ARCHIVE_MAGIC
from .hcb import encode_hcb
from .runner import execute
SEED=20260906

def fixtures(adapter):
 if adapter=='relational_bundle':
  from .workloads.fixtures import static_records
  return static_records()
 v=[StreamRecord('fixture-'+p.stem.replace('_','-'),p.read_bytes()) for p in sorted((Path(__file__).parent/'data/fixtures').glob('*.hcb'))]
 rng=random.Random(SEED);data=[b'',b'\0',bytes(range(256)),b'\xff\xfe\x00\x80',b'0'*10001,b'x'*1048576,b'\r\n\n\r',b'001.000\t-0001\t2025-02-31 24:00:60',b'no final newline']+[rng.randbytes(rng.randrange(65537)) for _ in range(24)]
 for i,b in enumerate(data):v.append(StreamRecord(f'generated-{i:04}',encode_hcb([('strange\nname-'+str(i),b)]) if adapter=='hcb1' else b))
 for name in ('a','b-0','different-prefix-0123','s-001','x'*4096,'z'*1048576):
  b=bytes(range(256))*3;v.append(StreamRecord(name,encode_hcb([('a',b)]) if adapter=='hcb1' else b))
 return tuple(sorted(v,key=lambda r:r.alias))

def corpus_fixtures(adapter,public):
 """Bounded byte-domain probes that exercise a corpus-specific fast path."""
 if adapter!='bytes':return ()
 values=[]
 def nested_order(value,top=False):
  if isinstance(value,dict):
   pairs=list(value.items())
   if not top:pairs.reverse()
   return {key:nested_order(child) for key,child in pairs}
  if isinstance(value,list):return [nested_order(child) for child in value]
  return value
 for i,record in enumerate(public[:2]):
  line=record.payload[:8192].split(b'\n',1)[0]
  variants=[('line',line+b'\n'),('no-newline',line),('leading-space',b' '+line+b'\n')]
  try:
   value=json.loads(line)
   variants.extend((name,json.dumps(value,separators=separators,ensure_ascii=True).encode()+b'\n')
                   for name,separators in [('json-compact',(',',':')),('json-spaced',(', ',': '))])
   variants.append(('nested-order',json.dumps(nested_order(value,True),separators=(',',':'),ensure_ascii=True).encode()+b'\n'))
  except (ValueError,UnicodeError,RecursionError):pass
  for name,payload in variants:
   if len(payload)<=16384:values.append(StreamRecord(f'corpus-probe-{i:02}-{name}',payload))
 return tuple(sorted(values,key=lambda row:row.alias))

def write_dir(p,records,magic):
 p.mkdir();(p/'names.bin').write_bytes(pack_stream(magic,[(r.alias,b'') for r in records]))
 for i,r in enumerate(records):(p/f'{i:08}.bin').write_bytes(r.payload)

def read_dir(p,magic):
 if any(x.is_symlink() or not x.is_file() for x in p.iterdir()):raise Error('invalid_directory_output')
 names=unpack_stream((p/'names.bin').read_bytes(),magic)
 if any(r.payload for r in names) or set(x.name for x in p.iterdir())!={'names.bin'}|{f'{i:08}.bin' for i in range(len(names))}:raise Error('directory_file_mismatch')
 return tuple(StreamRecord(r.alias,(p/f'{i:08}.bin').read_bytes()) for i,r in enumerate(names))

def copy_runtime(source,dest,excluded,limits):
 """Stage fresh files, with no link to an immutable candidate's writable state."""
 dest.mkdir()
 for path in source.rglob('*'):
  relative=path.relative_to(source)
  if path.is_file() and relative.as_posix() not in excluded:
   target=dest/relative;target.parent.mkdir(parents=True,exist_ok=True)
   candidate.checked_copy(path,target,limits);target.chmod(path.stat().st_mode&0o777)

def corruption_cases(enc):
 """Preserve the fixed case sequence without retaining every large mutation."""
 one=[enc[min(1,len(enc)-1)]];good=pack_stream(ARCHIVE_MAGIC,one);payload=one[0].payload
 yield b'';yield good[:1];yield good[:8];yield good[:-1];yield good+b'X';yield b'NOPE'+good[4:]
 for offset in (5,9):
  b=bytearray(good);b[offset:offset+4]=b'\xff'*4;yield bytes(b)
 for pos in sorted({0,1,4,5,8,15,16,23,24,27,len(payload)//2,len(payload)-1}):
  if 0<=pos<len(payload):
   b=bytearray(payload);b[pos]^=1;yield pack_stream(ARCHIVE_MAGIC,[(one[0].alias,bytes(b))])
 for n in sorted({0,1,7,16,len(payload)//2,len(payload)-1}):
  if 0<=n<len(payload):yield pack_stream(ARCHIVE_MAGIC,[(one[0].alias,payload[:n])])
 yield pack_stream(ARCHIVE_MAGIC,[(one[0].alias,payload+b'X')])
 rng=random.Random(SEED+1)
 for _ in range(8):yield rng.randbytes(rng.randrange(1,257))

class Evaluation:
 def __init__(self,root,rows,card,out,depth='full',mode='required',cancel=None,deadline=None):
  if ('resource_profiles' in card or card.get('accounting_policy')=='supervisor-decoder-v1') and '_resource_profile' not in card:card=dataset.select_profile(card)
  self.root=Path(root);self.rows=rows;self.card=card;self.out=Path(out);self.depth=depth;self.mode=mode;self.cancel=cancel;self.out.mkdir(parents=True,exist_ok=True)
  self.m,self.reg=candidate.verify(root);self.runtime=self.root/'runtime';self.dep=self.reg['dependencies'];self.sanitizer=False;self.n=0;self.started=time.perf_counter();self.deadline=deadline if deadline is not None else time.monotonic()+card['search_budget']['wall_seconds']
  self.decoder_runtime=self.root/('decoder-runtime' if self.m['schema_version']==2 else 'runtime');self.decoder_dep=self.reg.get('decoder_dependencies',self.dep)
  self.raw={'schema_version':1,'candidate_digest':self.reg['candidate_digest'],'depth':depth,'seed':SEED,'gates':{},'invocations':[],'timing_trials':{'encode':[],'decode':[]},'status':'running','fuzz':{}}
  self.raw.update(evaluation_mode=card.get('evaluation_mode','split'),accounting_policy=card.get('accounting_policy','standalone-v1'),timing_scope=card.get('timing_policy',{}).get('scope','train-plus-development-v1'),resource_profile=card.get('_resource_profile',{'id':'legacy-single','threads':1}),primary_resource_profile=card.get('_primary_profile','legacy-single'),declared_candidate_threads=self.m['threads'],diagnostic_kind=candidate.diagnostic_kind(self.m))
 def save(self):save(self.out/'raw.json',self.raw)
 def gate(self,name,value=True):
  if time.monotonic()>self.deadline:raise Error('wall_budget_exhausted')
  self.raw['gates'][name]=value;self.save()
 def invoke(self,op,stream=None,directory=None,check=True,short=False):
  remaining=self.deadline-time.monotonic()
  if remaining<=0:raise Error('wall_budget_exhausted')
  decoding=op.startswith('decode');preparing=op=='prepare_offline'
  runtime=self.preparation_runtime if preparing else self.decoder_runtime if decoding else self.runtime
  dep=self.decoder_dep if decoding else self.dep
  self.n+=1;call=self.tmp/f'call-{self.n:04}';call.mkdir();out=call/'out';out.mkdir();stdout=call/'stdout';ro={'/candidate':runtime}
  if directory:ro['/input']=directory
  commands=load(runtime/candidate.DECODER_COMMAND_FILE)['commands'] if decoding and self.m['schema_version']==2 else self.m['commands']
  command=self.m['offline']['command'] if preparing else commands[op]
  cmd=[a.replace('{runtime}','/candidate').replace('{input_dir}','/input').replace('{output_dir}','/output') for a in command]
  native_sanitizer=self.sanitizer and candidate.diagnostic_kind(self.m)=='cxx-asan-ubsan-v1'
  timeout=self.card['limits'].get('offline_timeout_seconds',self.card['limits']['timeout_seconds']) if preparing else self.card['limits']['timeout_seconds']
  try:r=execute(cmd,output=out,readonly=ro,stdin=stream,stdout=stdout,check=check,timeout=min(remaining,5 if short else timeout,timeout),memory=self.card['limits']['memory_bytes'],output_limit=self.card['limits'].get('output_bytes',8*1024**3),sanitizer=native_sanitizer,mode=self.mode,cancel=self.cancel,runtime_files=candidate.runtime_mounts(dep,native_sanitizer),threads=min(self.m['threads'],self.card['limits']['threads']),cpus=self.card['limits'].get('cpus'))
  except Error as e:
   if hasattr(e,'measurement'):self.raw['invocations'].append({'phase':op,'sanitizer':self.sanitizer,**e.measurement});self.save()
   raise
  self.raw['invocations'].append({'phase':op,'sanitizer':native_sanitizer,'diagnostic_kind':candidate.diagnostic_kind(self.m) if self.sanitizer else None,'decoder_bundle_only':decoding and self.m['schema_version']==2,**r})
  if 'AddressSanitizer' in r['stderr'] or 'runtime error:' in r['stderr']:raise Error('sanitizer_failure',r['stderr'][:2000])
  if self.sanitizer and candidate.diagnostic_kind(self.m)=='rust-checked-v1' and 'panicked at' in r['stderr']:raise Error('rust_checked_failure',r['stderr'][:2000])
  return stdout,out,r
 def stream(self,records,encode=True):
  p=self.tmp/f'input-{self.n}.bin';p.write_bytes(pack_stream(ORIGINAL_MAGIC if encode else ARCHIVE_MAGIC,records));out,_,r=self.invoke('encode_stream' if encode else 'decode_stream',stream=p)
  records=unpack_stream(out.read_bytes(),ARCHIVE_MAGIC if encode else ORIGINAL_MAGIC);p.unlink();shutil.rmtree(out.parent)
  return records,None,r
 def exact(self,records,label):
  enc,_,_=self.stream(records);dec,_,_=self.stream(enc,False)
  if dec!=tuple(records):
   wrong=next((a for a,b in zip(records,dec) if a!=b),records[0]);(self.out/'first-failure-input.hbi').write_bytes(pack_stream(ORIGINAL_MAGIC,[wrong]));raise Error('byte_mismatch',label)
  if [r.alias for r in enc]!=[r.alias for r in records]:raise Error('stream_name_mismatch',label)
  return enc
 def parity(self,records,enc,label):
  p=self.tmp/(label+'-directory');write_dir(p,records,ORIGINAL_MAGIC);_,e,_=self.invoke('encode_dir',directory=p)
  if read_dir(e,ARCHIVE_MAGIC)!=tuple(enc):raise Error('directory_stream_parity',label)
  _,d,_=self.invoke('decode_dir',directory=e)
  if read_dir(d,ORIGINAL_MAGIC)!=tuple(records):raise Error('directory_byte_mismatch',label)
  shutil.rmtree(p);shutil.rmtree(e.parent);shutil.rmtree(d.parent)
 def corruption(self,enc,sanitized=False,scope='corpus'):
  cases=corruption_cases(enc)
  start=time.perf_counter();tested=0
  for b in islice(cases,12) if sanitized else cases:
   p=self.tmp/f'corrupt-{self.n}.input';p.write_bytes(b);o,_,r=self.invoke('decode_stream',stream=p,check=False,short=True)
   if r['returncode']==0 or o.stat().st_size:
    self.raw['corruption_scope']=scope
    (self.out/'corruption-reproducer.hba').write_bytes(b);raise Error('corruption_accepted_or_partial_output',scope)
   p.unlink();shutil.rmtree(o.parent);tested+=1
  key=('' if scope=='corpus' else scope+'_')+('sanitized_invalid_cases' if sanitized else 'invalid_cases')
  self.raw['fuzz'][key]={'count':tested,'elapsed_seconds':time.perf_counter()-start,'per_case_timeout_seconds':min(5,self.card['limits']['timeout_seconds'])}
 def prepare_offline(self):
  """Replay the declared fitter from raw inputs and use its verified fresh output."""
  offline=self.m['offline'];paths=set(offline['artifact_paths'])
  timeout=self.card['limits'].get('offline_timeout_seconds',self.card['limits']['timeout_seconds'])
  limits={**self.card['limits'],'timeout_seconds':timeout,
          '_deadline':min(self.deadline,time.monotonic()+timeout),'_cancel':self.cancel}
  fitting=sorted((r for r in self.rows if r['split']==dataset.fitting_partition(self.card)),
                 key=lambda r:(r['canonical_sha256'],r['alias']))
  if not fitting:raise Error('missing_offline_fitting_data')
  inputs=self.tmp/'offline-input'
  if not inputs.exists():
   inputs.mkdir();manifest=[]
   for i,row in enumerate(fitting):
    name=f'{i:08}.bin';candidate.checked_copy(row['source'],inputs/name,limits)
    manifest.append({**{k:row[k] for k in ('alias','canonical_bytes','canonical_sha256','split')},'path':name})
   save(inputs/'manifest.json',{'schema_version':1,'objects':manifest},0o444)
  template=self.tmp/('offline-template-diagnostic' if self.sanitizer else 'offline-template')
  if not template.exists():
   excluded=paths|(set(self.m['build_output_paths'])-{offline['executable']})
   copy_runtime(self.runtime,template,excluded,limits)
  self.preparation_runtime=template
  stdout,outputs,measurement=self.invoke('prepare_offline',directory=inputs)
  actual=candidate.scan(outputs,paths,limits)
  expected=[r for r in self.reg['source_files'] if r['path'] in paths]
  if actual!=expected:raise Error('offline_artifact_mismatch')
  if stdout.stat().st_size:raise Error('unexpected_offline_stdout')
  record=self.raw.setdefault('offline_preparation',{'fitting_partition':dataset.fitting_partition(self.card),
      'fitting_canonical_bytes':sum(r['canonical_bytes'] for r in fitting),
      'fitting_objects':[r['canonical_sha256'] for r in fitting],
      'artifact_checks':[],'dataset_dependent_rebuild':offline['rebuild']})
  record['artifact_checks'].append({'invocation':self.n,'diagnostic':self.sanitizer,
                                  'artifact_digest':digest(actual),'files':actual})
  prepared=outputs.parent/'prepared-runtime';build_ns=0;peak=measurement['peak_rss_bytes']
  if offline['rebuild'] and not self.sanitizer:
   source=outputs.parent/'prepared-source';copy_runtime(self.root/'source',source,paths,limits)
   for name in paths:
    target=source/name;target.parent.mkdir(parents=True,exist_ok=True)
    candidate.checked_copy(outputs/name,target,limits)
   builddir=outputs.parent/'prepared-build'
   build_cpus=limits.get('cpus')
   built=candidate.build(source,self.m,builddir,self.mode,limits=limits,cancel=self.cancel,
                         cpus=build_cpus[:1] if build_cpus else None)
   if built['files']!=self.reg['build_files'] or built['dependencies']!=self.reg['dependencies'] or built['decoder_dependencies']!=self.reg['decoder_dependencies']:
    raise Error('nonreproducible_offline_build')
   for row in built['logs']:
    self.raw['invocations'].append({'phase':'offline_build','sanitizer':False,**row})
   build_ns=sum(r['elapsed_ns'] for r in built['logs'])
   peak=max([peak]+[r['peak_rss_bytes'] for r in built['logs']])
   candidate.make_runtime(source,builddir,self.m,built,prepared,limits=limits)
  else:
   copy_runtime(self.runtime,prepared,paths,limits)
   for name in paths:
    target=prepared/name;target.parent.mkdir(parents=True,exist_ok=True)
    candidate.checked_copy(outputs/name,target,limits);target.chmod(0o444)
  self.save()
  return prepared,{**measurement,'elapsed_ns':measurement['elapsed_ns']+build_ns,
                   'preparation_elapsed_ns':measurement['elapsed_ns'],'build_elapsed_ns':build_ns,
                   'peak_rss_bytes':peak,'artifact_digest':digest(actual),
                   'canonical_bytes':record['fitting_canonical_bytes']}

 def timed(self,public,enc):
  inp=self.tmp/'timing-original.hbi';arc=self.tmp/'timing-archive.hba';inp.write_bytes(pack_stream(ORIGINAL_MAGIC,public));arc.write_bytes(pack_stream(ARCHIVE_MAGIC,enc))
  sourcehash=sha(inp);archash=sha(arc);canonical_bytes=sum(len(r.payload) for r in public)
  staged=self.card.get('timing_policy',{}).get('operation')=='offline-plus-online-v1'
  if staged:self.raw['timing_trials']['offline']=[]
  for phase,op,file,expected in [('encode','encode_stream',inp,archash),('decode','decode_stream',arc,sourcehash)]:
   for trial in range(7):
    runtime=self.runtime;prepared=None
    try:
     if phase=='encode' and staged and self.m.get('offline'):
      prepared,offline=self.prepare_offline();self.runtime=prepared
      self.raw['timing_trials']['offline'].append({**offline,'trial':trial+1})
      self.save()
     out,_,r=self.invoke(op,stream=file);actual=sha(out)
    finally:self.runtime=runtime
    self.raw['timing_trials'][phase].append({**r,'trial':trial+1,'canonical_bytes':canonical_bytes,'output_sha256':actual})
    self.save()
    if actual!=expected:raise Error('nondeterministic_timed_output',phase)
    shutil.rmtree(out.parent)
    if prepared:shutil.rmtree(prepared.parent)
  if staged:
   self.raw['timing_trials']['combined']=accounting.combine_encoding_trials(self.raw['timing_trials']['offline'],self.raw['timing_trials']['encode'])
  self.raw['timing']={p:accounting.timing(t,p) for p,t in self.raw['timing_trials'].items() if t}
  if staged:
   self.raw['timing']['encode']['stage']='online'
   self.raw['timing'].setdefault('offline',{'used':False,'trials':0,'median_seconds':0,'peak_rss_bytes':0})
   if self.m.get('offline'):self.raw['timing']['offline']['used']=True
   self.gate('offline_artifact_reproduction',{'used':bool(self.m.get('offline')),'trials':7 if self.m.get('offline') else 0})
  self.gate('fresh_process_seven_trials')
 def run(self):
  try:
   with tempfile.TemporaryDirectory(prefix='compression-lab-gates-') as td:
    self.tmp=Path(td)
    staged=self.card.get('timing_policy',{}).get('operation')=='offline-plus-online-v1'
    if self.m.get('offline') and not staged:raise Error('offline_requires_staged_timing_policy')
    if staged and self.m.get('training',{}).get('kind') in ('train-only','whole-dataset') and not self.m.get('offline'):
     raise Error('offline_preparation_required','Declare a reproducible offline command for fitted artifacts or data-dependent code generation')
    if self.depth=='full':
     b=candidate.build(self.root/'source',self.m,self.tmp/'rebuild',self.mode,limits={**self.card['limits'],'_deadline':self.deadline},cancel=self.cancel);self.raw['rebuild']=b
     if b['files']!=self.reg['build_files'] or b['dependencies']!=self.reg['dependencies']:raise Error('nonreproducible_build')
     if self.m['schema_version']==2 and b['decoder_dependencies']!=self.reg['decoder_dependencies']:raise Error('nonreproducible_decoder_dependencies')
     self.gate('clean_rebuild_exact_binary')
    rows=self.rows if self.depth=='full' else self.rows[:min(2,len(self.rows))]
    public=tuple(sorted([StreamRecord(r['alias'],Path(r['source']).read_bytes()) for r in rows],key=lambda r:r.alias))
    enc=self.exact(public,'public');self.parity(public,enc,'public');self.gate('public_exactness_and_directory_stream_parity',{'objects':len(public)})
    corpus_fx=corpus_fixtures(self.card['adapter'],public)
    fx=tuple(sorted((*fixtures(self.card['adapter']),*corpus_fx),key=lambda row:row.alias))
    fenc=self.exact(fx,'mandatory-fixtures');self.parity(fx,fenc,'fixtures');self.gate('valid_unusual_inputs',{'supplied_fixtures':0 if self.card['adapter']=='relational_bundle' else 35,'corpus_derived_cases':len(corpus_fx),'total_cases':len(fx),'max_alias_bytes':1048576,'generated_seed':SEED})
    again,_,_=self.stream(public)
    if again!=enc:raise Error('nondeterministic_archive')
    del again
    # Reorder bytes under fresh legal aliases; archives must not depend on names/order.
    perm=tuple(StreamRecord(f'reordered-{i:06}',r.payload) for i,r in enumerate(reversed(public)));penc,_,_=self.stream(perm)
    if [r.payload for r in penc]!=[r.payload for r in reversed(enc)]:raise Error('cross_object_or_filename_state')
    del perm,penc
    for i in range(len(public)):
     one,_,_=self.stream([public[i]])
     if one!=(enc[i],):raise Error('cross_object_encoding_state')
     dec,_,_=self.stream([enc[i]],False)
     if dec!=(public[i],):raise Error('object_not_independent')
     del one,dec
    self.gate('deterministic_independent_reordered_subsets')
    sizes=[];byalias={r.alias:len(r.payload) for r in enc}
    for row in rows:sizes.append({k:row[k] for k in ('alias','group','split','canonical_bytes')}|{'archive_payload_bytes':byalias[row['alias']],'record_framing_bytes':12+len(row['alias'].encode('ascii')),'archive_bytes':byalias[row['alias']]+12+len(row['alias'].encode('ascii'))})
    self.raw['objects']=sizes;fixed=candidate.costs(self.m,self.reg);self.raw['fixed_costs']=fixed
    self.raw['accounting']={split:accounting.costs(sum(r['canonical_bytes'] for r in sizes if split=='all' or r['split']==split),sum(r['archive_bytes'] for r in sizes if split=='all' or r['split']==split)+(9 if any(split=='all' or r['split']==split for r in sizes) else 0),fixed,self.card['objective']['deployment_canonical_bytes']) for split in ('all',*dataset.public_partitions(self.card))}
    self.raw['decoder_accounting']={split:accounting.decoder_costs(v['canonical_bytes'],v['actual']['archive_bytes'],fixed,self.card['objective']['deployment_canonical_bytes']) for split,v in self.raw['accounting'].items()}
    self.raw['primary_size_policy']=self.card.get('primary_size_policy','strict-deployment-v1')
    self.raw['timing_operation']=self.card.get('timing_policy',{}).get('operation','legacy-fit-excluded-v1')
    if staged:self.raw['encoding_floor_scope']=self.card['timing_policy']['encoding_floor_scope']
    if self.card.get('primary_size_policy')=='standard-codec-available-v1':
     self.raw['reported_accounting']=deployment.decoder_views(fixed,self.raw['decoder_accounting'],self.card['runtime_scenarios'])
    if self.card.get('accounting_policy')=='supervisor-decoder-v1':
     if self.m['schema_version']!=2:raise Error('decoder_policy_requires_v2_manifest','Use explicit role membership and an independently staged decoder runtime')
     self.gate('decoder_bundle_exactness',{'files':self.reg['decoder_runtime_files'],'encoder_mounted_for_decode':False})
    self.raw['deployment_views']=deployment.views(fixed,self.raw['accounting'],self.card.get('runtime_scenarios'))
    self.raw['transport']={'hbi_input_bytes':len(pack_stream(ORIGINAL_MAGIC,public)),'hba_archive_bytes':len(pack_stream(ARCHIVE_MAGIC,enc)),'batch_header_bytes':9,'record_framing_bytes':sum(r['record_framing_bytes'] for r in sizes),'archive_payload_bytes':sum(r['archive_payload_bytes'] for r in sizes),'scoring_policy':'Archive costs include all HBA/ordinal-directory names-index framing, not merely compressed payload. Each reported partition includes its own nine-byte batch header.'}
    self.gate('complete_cost_inventory')
    if self.depth=='full':
     self.corruption(enc);self.gate('bounded_corruption_rejection')
     self.corruption(fenc,scope='fixtures');self.gate('bounded_fixture_corruption_rejection')
     # Reject malformed directory archives without leaving successful partial outputs.
     bad=self.tmp/'bad-directory';write_dir(bad,enc,ARCHIVE_MAGIC);first=bad/'00000000.bin';first.write_bytes(first.read_bytes()[:-1]);_,out,r=self.invoke('decode_dir',directory=bad,check=False,short=True)
     if r['returncode']==0 or any(out.iterdir()):raise Error('directory_corruption_accepted')
     self.gate('directory_corruption_rejection')
     sb=candidate.build(self.root/'source',self.m,self.tmp/'sanitized-build',self.mode,True,{**self.card['limits'],'_deadline':self.deadline},self.cancel);self.raw['sanitizer_build']=sb
     # Require sanitizer runtime evidence, not an unchecked manifest assertion.
     names=[r['soname'] for r in sb['dependencies']['libraries']]
     if candidate.diagnostic_kind(self.m)=='cxx-asan-ubsan-v1' and (not any(n.startswith('libasan.so') for n in names) or not any(n.startswith('libubsan.so') for n in names)):raise Error('blocked_toolchain','C/C++ diagnostics require dynamic ASan and UBSan runtimes')
     candidate.make_runtime(self.root/'source',self.tmp/'sanitized-build',self.m,sb,self.tmp/'sanitized-runtime');self.runtime=self.tmp/'sanitized-runtime';self.dep=sb['dependencies'];self.sanitizer=True
     if self.m['schema_version']==2:
      candidate.make_runtime(self.root/'source',self.tmp/'sanitized-build',self.m,sb,self.tmp/'sanitized-decoder-runtime',decoder=True);self.decoder_runtime=self.tmp/'sanitized-decoder-runtime';self.decoder_dep=sb['decoder_dependencies']
     else:self.decoder_runtime=self.runtime;self.decoder_dep=self.dep
     if staged and self.m.get('offline'):self.prepare_offline();self.gate('offline_diagnostics')
     se=self.exact(fx,'asan-ubsan-fixtures');self.parity(fx,se,'sanitized');self.corruption(se,True)
     if candidate.diagnostic_kind(self.m)=='cxx-asan-ubsan-v1':self.gate('asan_ubsan',{'fixtures':len(fx),'leak_detection':False,'coverage_guided':False,'dependency_instrumentation':'Precompiled shared/static codec and platform libraries are not rebuilt with sanitizer instrumentation. Candidate compilation and boundary accesses are checked.','policy':'Engine-linked default ASan/UBSan options; leak detection disabled because host /proc is not mounted.','scope':'Bounded deterministic valid-input properties and corruptions. Not a proof or a coverage-guided libFuzzer run.'})
     else:self.gate('rust_checked',{'fixtures':len(fx),'kind':'rust-checked-v1','ASan':False,'UBSan':False,'scope':'Stable direct rustc with debug assertions and overflow checks enabled. Does not diagnose unsafe memory accesses, FFI, races or all undefined behavior.'})
     self.runtime=self.root/'runtime';self.dep=self.reg['dependencies'];self.decoder_runtime=self.root/('decoder-runtime' if self.m['schema_version']==2 else 'runtime');self.decoder_dep=self.reg.get('decoder_dependencies',self.dep);self.sanitizer=False
     if self.card.get('timing_policy',{}).get('scope')=='validation-only-v2':
      names={r['alias'] for r in rows if r['split']=='development'};timed_public=tuple(r for r in public if r.alias in names);timed_enc=tuple(r for r in enc if r.alias in names)
      if not timed_public:raise Error('missing_validation_timing_data')
      self.timed(timed_public,timed_enc)
     else:self.timed(public,enc)
    candidate.verify(self.root);self.gate('end_digest_exact')
    self.raw['quality_passed']=True;reasons=[]
    if self.depth!='full':reasons+=['quick_not_promotable']
    else:
     if runner.cgroup_limits()['status']!='verified':reasons+=['effective_cgroup_limits_unavailable']
     if self.card.get('resource_profiles'):
      limits=runner.cgroup_limits();cores=min(self.card['limits']['threads'],len(self.card['limits']['cpus']))
      if limits.get('cpu_quota_cores') is None or limits['cpu_quota_cores']<cores:reasons+=['declared_cpu_budget_not_commissioned']
      if limits.get('memory_max_bytes') is None:reasons+=['finite_enclosing_memory_limit_required']
     primary=self.card.get('_resource_profile',{}).get('id','legacy-single')==self.card.get('_primary_profile','legacy-single')
     self.raw['encoding_floor_applied']=primary
     encoding=self.raw['timing'][dataset.encoding_timing_key(self.card)]
     if not primary:reasons+=['reference_profile_not_promotable']
     elif encoding['median_bytes_per_second']<100000000:reasons+=['encoding_below_100_MBps']
     if encoding['relative_MAD']>self.card.get('timing_policy',{}).get('noise_relative_mad_limit',.1):reasons+=['timing_noisy_logged_rerun_required']
    if self.card.get('timing_policy',{}).get('role','smoke')!='benchmark':reasons+=['smoke_not_certified']
    if self.mode!='required':reasons+=['unsandboxed_exploratory']
    self.raw.update(status='eligible' if not reasons else 'ineligible',eligible=not reasons,reason_codes=reasons)
  except BaseException as e:
   self.raw.update(status='cancelled' if getattr(e,'code',None)=='cancelled' else 'failed',quality_passed=False,eligible=False,reason_codes=[getattr(e,'code','evaluation_error')],error=str(e)[:4000])
  self.raw['wall_seconds']=time.perf_counter()-self.started;self.save();return self.raw
