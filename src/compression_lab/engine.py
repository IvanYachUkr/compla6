"""Transport-independent experiment engine, durable jobs and immutable evidence."""
from __future__ import annotations
import contextlib,hashlib,importlib.metadata,io,json,os,random,shlex,shutil,subprocess,sys,tempfile,time,uuid,zipfile
from pathlib import Path
from .util import Error,Ledger,load,save,sha,digest,canonical,lock,now,ident,reply,safe
from . import dataset,candidate,runner,baselines,accounting
from .workloads import bridge

_PROCESSES={}

class Engine:
 def __init__(self,workspace):
  self.root=Path(workspace).absolute();self.ledger=Ledger(self.root/'state')
  if not self.ledger.journal.exists():raise Error('workspace_not_initialized')
 @classmethod
 def init(cls,workspace,card_path,exploratory=False):
  root=Path(workspace).absolute()
  if root.exists() and any(root.iterdir()):raise Error('workspace_not_empty')
  root.mkdir(parents=True,exist_ok=True)
  if not exploratory and runner.probe()['status']!='available':raise Error('sandbox_unavailable','Use --exploratory only for public non-release work')
  c,cardhash=dataset.snapshot(card_path,root)
  # Resource allocation is part of an evaluation's identity.  Pin every
  # declared profile now, with the primary profile remaining the workspace
  # runtime fingerprint used by legacy callers and registrations.
  profile_ids=([x['id'] for x in c['resource_profiles']['profiles']]
               if c.get('resource_profiles') else ['legacy-single'])
  pinned={}
  for profile_id in profile_ids:
   selected=dataset.select_profile(c,profile_id);resource=selected['_resource_profile']
   runtime=runner.fingerprint(threads=resource['threads'],cpus=resource['cpus'])
   pinned[profile_id]={'resource_profile':resource,'resource_profile_digest':digest(resource),'runtime':runtime}
  primary=dataset.primary_profile(c);runtime=pinned[primary]['runtime']
  save(root/'runtime.json',runtime)
  save(root/'runtime-profiles.json',{'schema_version':1,'card_digest':cardhash,'primary':primary,'profiles':pinned},0o444)
  for d in ('jobs','results','candidates','workbench','exports'): (root/d).mkdir()
  l=Ledger(root/'state');rid='run-'+uuid.uuid4().hex
  l.create({'stage':'created','run_id':rid,'card_digest':cardhash,'runtime_digest':runtime['runtime_digest'],'runtime_profiles_digest':digest(pinned),'mode':'exploratory' if exploratory else 'required','created_at':now(),'active_job':None,'registered':[],'results':[],'budgets':{},'search_budget':c['search_budget']})
  l.transition('public_search');return cls(root)
 def state(self):return self.ledger.read()
 def researcher_view(self):return ResearcherEngine(self.root)
 def _controller(self):
  from .controller import Controller
  return Controller(self) if Controller.exists(self) else None
 def _guard_research(self):
  controller=self._controller()
  if controller:controller.guard_research()
 @contextlib.contextmanager
 def _research_operation(self,kind,track):
  self.recover_operation()
  with lock(self.root/'operation.lock',nonblocking=True):
   with lock(self.root/'admission.lock'):
    self._guard_research()
    operation={'id':'op-'+uuid.uuid4().hex,'kind':kind,'track':track,'created_epoch':time.time(),
     'pid':os.getpid(),'proc_start':Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()[19]}
    self.ledger.update('operation_started',lambda s:{**s,'active_operation':operation})
   error=None
   try:yield
   except BaseException as ex:error=getattr(ex,'code',type(ex).__name__);raise
   finally:
    def end(s):
     active=s.get('active_operation')
     if active and active['id']==operation['id']:
      s['operation_history']=(s.get('operation_history',[])+[{**active,'status':'failed' if error else 'complete','reason_code':error}])[-32:]
      s['active_operation']=None
     return s
    self.ledger.update('operation_finished',end)
    controller=self._controller()
    if controller:controller.reconcile()
 def recover_operation(self):
  active=self.state().get('active_operation')
  if not active or self._alive(active):return
  with lock(self.root/'operation.lock',nonblocking=True):
   def recover(s):
    active=s.get('active_operation')
    if not active or self._alive(active):return s
    if not active.get('charged'):
     elapsed=max(0,time.time()-active['created_epoch']);budget=s['budgets'].setdefault(active['track'],{'evaluations':0,'wall_seconds':0})
     budget['wall_seconds']+=elapsed;budget['evaluations']+=1
     active.update(charged=True,charged_wall_seconds=elapsed)
    s['operation_history']=(s.get('operation_history',[])+[{**active,'status':'failed','reason_code':'operation_worker_crashed'}])[-32:]
    s['active_operation']=None;return s
   self.ledger.update('operation_recovery',recover)
 def _search_limits(self,started,budget,card):
  deadline=started+self.state()['search_budget']['wall_seconds']-budget['wall_seconds'];controller=self._controller()
  limits={**card['limits'],'_deadline':deadline}
  if controller:
   epoch=controller.state()['protocol']['deadline_epoch']
   if epoch is not None:limits['_deadline']=min(deadline,time.monotonic()+max(0,epoch-time.time()))
   limits['_cancel']=self.root/'controller'/'cancel.json'
  return limits
 def data(self):
  s=self.state();return dataset.read_snapshot(self.root,s['card_digest'])
 def metadata(self):
  return dataset.read_metadata(self.root,self.state()['card_digest'])
 def _profile_context(self,profile_id=None):
  """Return the sealed effective card and its pinned runtime identity."""
  state=self.state();card,_=self.metadata();selected=dataset.select_profile(card,profile_id)
  resource=selected['_resource_profile'];name=resource['id']
  saved=load(safe(self.root,'runtime-profiles.json'))
  if (saved.get('schema_version')!=1 or saved.get('card_digest')!=state['card_digest']
      or saved.get('primary')!=dataset.primary_profile(card) or not isinstance(saved.get('profiles'),dict)):
   raise Error('stale_resource_profile_fingerprint')
  if state.get('runtime_profiles_digest') and digest(saved['profiles'])!=state['runtime_profiles_digest']:
   raise Error('stale_resource_profile_fingerprint')
  entry=saved['profiles'].get(name)
  if not isinstance(entry,dict) or entry.get('resource_profile')!=resource or entry.get('resource_profile_digest')!=digest(resource):
   raise Error('stale_resource_profile_fingerprint',name)
  runtime=entry.get('runtime')
  if not isinstance(runtime,dict) or digest({k:v for k,v in runtime.items() if k!='runtime_digest'})!=runtime.get('runtime_digest'):
   raise Error('invalid_runtime_fingerprint')
  if name==saved['primary'] and runtime.get('runtime_digest')!=state['runtime_digest']:
   raise Error('stale_runtime_digest')
  return selected,entry
 def profile(self):
  s=self.state();return reply(s['run_id'],metrics=dataset.profile(self.root,s['card_digest']))
 def record_hypothesis(self,statement,expected_benefit,falsifier):
  from .instructions import record_hypothesis
  with lock(self.root/'admission.lock'):
   return reply(self.state()['run_id'],metrics=record_hypothesis(self,statement,expected_benefit,falsifier))
 def register(self,path,track='agent'):
  with self._research_operation('registration',track):return self._register(path,track)
 def _register(self,path,track):
  self._guard_research()
  if track not in ('agent','baseline','deterministic','random'):raise Error('invalid_track')
  s=self.state()
  if s['stage'] not in ('public_search','public_ready'):raise Error('terminal_latch')
  if s['active_job']:raise Error('job_active')
  budget=s['budgets'].get(track,{'evaluations':0,'wall_seconds':0})
  if budget['evaluations']>=s['search_budget']['candidate_evaluations'] or budget['wall_seconds']>=s['search_budget']['wall_seconds']:raise Error('budget_exhausted')
  started=time.monotonic();c,rows=self.metadata();failure=None;r=None;cache_info={}
  limits=self._search_limits(started,budget,c)
  try:
   if track=='agent' and c.get('hypothesis_policy')=='required':
    from .instructions import verify_hypothesis
    from .util import secure_load
    hypothesis_before=verify_hypothesis(self,secure_load(Path(path),'candidate.json'))
   if runner.require_fingerprint(load(self.root/'runtime.json'),limits=limits)['runtime_digest']!=s['runtime_digest']:raise Error('stale_runtime_digest')
   with lock(runner.HOST_LOCK,host=True,nonblocking=True):r=bridge.register_candidate(self.root,Path(path),s['mode'],limits,[x['canonical_sha256'] for x in rows if x['split']==dataset.fitting_partition(c)],c,cache_info)
   if track=='agent' and c.get('hypothesis_policy')=='required':
    if verify_hypothesis(self,load(self.root/'candidates'/r['candidate_digest']/'source/candidate.json'))!=hypothesis_before:raise Error('hypothesis_changed_during_registration')
  except BaseException as ex:
   failure={'reason_code':getattr(ex,'code','registration_failed'),'error':str(ex)[:2000]}
   if hasattr(ex,'measurement'):failure['measurement']=ex.measurement
   raise
  finally:
   elapsed=time.monotonic()-started
   def charge(s):
    b=s['budgets'].setdefault(track,{'evaluations':0,'wall_seconds':0});b['wall_seconds']+=elapsed
    if s.get('active_operation'):s['active_operation'].update(charged=True,charged_wall_seconds=elapsed)
    if failure:
     if failure['reason_code']!='busy':b['evaluations']+=1
     s.setdefault('registration_failures',[]).append({'track':track,'wall_seconds':elapsed,**failure})
    return s
   self.ledger.update('registration_attempt',charge)
  def change(s):
   s['registered']=sorted(set(s['registered']+[r['candidate_digest']]));s['candidate_digest']=r['candidate_digest']
   s.setdefault('registrations',[]).append({'candidate_digest':r['candidate_digest'],'track':track})
   return s
  self.ledger.update('register',change)
  return reply(s['run_id'],candidate_digest=r['candidate_digest'],metrics={'source_digest':r['source_digest'],'source_package_bytes':r['source_package']['bytes'],'build_bytes':sum(x['bytes'] for x in r['build_files']),'registration_wall_seconds':elapsed,'registration_cache_hit':cache_info.get('hit',False)})
 def evaluate(self,cid,depth='full',track='agent',wait=False,workload=None,resource_profile=None,request_id=None):
  self.recover_operation()
  with lock(self.root/'operation.lock',nonblocking=True),lock(self.root/'admission.lock'):
   queued=self._evaluate(cid,depth,track,workload,resource_profile,request_id)
  return self.wait(queued['job_id']) if wait else queued
 def _evaluate(self,cid,depth,track,workload=None,resource_profile=None,request_id=None):
  if depth not in ('screen','quick','full') or track not in ('agent','baseline','deterministic','random'):raise Error('invalid_evaluation_mode')
  signature=digest([cid,depth,track,workload,resource_profile])
  if request_id is not None:
   ident(request_id);previous=self.state().get('evaluation_requests',{}).get(request_id)
   if previous:
    if previous['signature']!=signature:raise Error('request_id_conflict')
    return reply(self.state()['run_id'],'queued',job_id=previous['job_id'],candidate_digest=cid,metrics={'replayed_request':True})
  self._guard_research()
  if cid not in self.state()['registered']:raise Error('candidate_not_registered')
  ident(cid);m,r=bridge.verify_candidate(self.root/'candidates'/cid);c,profile_pin=self._profile_context(resource_profile);workload=bridge.select_workload(c,m,workload);job='job-'+uuid.uuid4().hex
  runner.require_fingerprint(profile_pin['runtime'])
  if depth=='screen' and workload=='mutable_store':raise Error('screen_requires_public_static_workload')
  j={'schema_version':1,'run_id':self.state()['run_id'],'job_id':job,'candidate_digest':cid,'depth':depth,'track':track,'workload':workload,
     'resource_profile':c['_resource_profile']['id'],'resource_profile_digest':profile_pin['resource_profile_digest'],
     'resource_profile_details':profile_pin['resource_profile'],
     'resource_runtime_digest':profile_pin['runtime']['runtime_digest'],'primary_resource_profile':c['_primary_profile'],
     'evaluation_mode':c.get('evaluation_mode','split'),'timing_scope':c.get('timing_policy',{}).get('scope','train-plus-development-v1'),
     'accounting_policy':c.get('accounting_policy','standalone-v1'),
     'status':'queued','created_at':now(),'created_epoch':time.time(),'pid':None,'result_id':None,
     'budget_timing_version':2,'request_id':request_id}
  def reserve(s):
   if cid not in s['registered']:raise Error('candidate_not_registered')
   if s['stage'] not in ('public_search','public_ready'):raise Error('terminal_latch')
   if s['active_job']:raise Error('job_active')
   b=s['budgets'].setdefault(track,{'evaluations':0,'wall_seconds':0})
   if b['evaluations']>=s['search_budget']['candidate_evaluations'] or b['wall_seconds']>=s['search_budget']['wall_seconds']:raise Error('budget_exhausted')
   b['evaluations']+=1;s['active_job']=job;s['pending_job']=j.copy();s['candidate_digest']=cid
   if request_id is not None:s.setdefault('evaluation_requests',{})[request_id]={'signature':signature,'job_id':job}
   return s
  s=self.ledger.update('reserve_evaluation',reserve);jd=self.root/'jobs'/job;jd.mkdir()
  save(jd/'job.json',j)
  # Isolated Python import path: no project module can shadow the trusted engine.
  code='import sys;sys.path.insert(0,'+repr(str(Path(__file__).parent.parent))+');from compression_lab.worker import main;main()'
  worker_env={'PATH':'/usr/bin:/bin','HOME':str(jd)}
  for name in ('COMPRESSION_LAB_RUST_TOOLCHAIN','COMPRESSION_LAB_NATIVE_PREFIX'):
   if os.environ.get(name):worker_env[name]=os.environ[name]
  try:
   with open(jd/'worker.log','wb') as log:
    _PROCESSES[job]=subprocess.Popen([sys.executable,'-I','-S','-c',code,str(self.root),job],stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,start_new_session=True,env=worker_env)
  except OSError as e:
   self._finish_job(j,{'status':'failed','quality_passed':False,'eligible':False,'reason_codes':['worker_spawn_failed'],'error':str(e),'wall_seconds':0,'candidate_digest':cid})
  return reply(s['run_id'],'queued',job_id=job,candidate_digest=cid,metrics={'resource_profile':j['resource_profile'],'resource_profile_digest':j['resource_profile_digest'],'timing_scope':j['timing_scope'],'accounting_policy':j['accounting_policy']})
 def wait(self,job,timeout=None):
  start=time.monotonic()
  while True:
   r=self.status(job=job)
   if r['status'] not in ('queued','running'):
    p=_PROCESSES.pop(job,None)
    if p is not None:p.wait(timeout=5)
    return r
   if timeout is not None and time.monotonic()-start>timeout:raise Error('wait_timeout','The durable job continues; query status or cancel')
   time.sleep(.05)
 def _job(self,job):
  ident(job);p=self.root/'jobs'/job/'job.json'
  if not p.exists():
   pending=self.state().get('pending_job')
   if pending and pending['job_id']==job:save(p,pending)
  return load(safe(self.root/'jobs'/job,'job.json'))
 def _alive(self,j):
  if not j.get('pid'):return time.time()-j['created_epoch']<30
  try:
   fields=Path(f"/proc/{j['pid']}/stat").read_text().rsplit(')',1)[1].split()
   return fields[0] not in ('Z','X') and fields[19]==j.get('proc_start')
  except (OSError,IndexError):return False
 def status(self,job=None,result=None):
  s=self.state()
  if result:return self.result(result)
  if job:
   j=self._job(job)
   if not j.get('result_id'):
    committed=self._committed_job(job,s)
    if committed:
     j.update(committed);save(self.root/'jobs'/job/'job.json',j)
   if j['status'] in ('queued','running') and not self._alive(j):
    # Consume a crashed attempt; never silently restart it or erase partial evidence.
    with lock(self.root/'jobs'/job/'reconcile.lock'):
     j=self._job(job)
     if j['status'] in ('queued','running') and not self._alive(j):
      elapsed=max(0,time.time()-j['created_epoch'])
      recovered={'schema_version':1,'candidate_digest':j['candidate_digest'],'status':'failed','quality_passed':False,'eligible':False,'reason_codes':['worker_crashed'],'partial_evidence_preserved':True,'wall_seconds':elapsed}
      if j.get('budget_timing_version')==2:
       active=max(0,time.time()-j['active_started_epoch']) if j.get('active_started_epoch') is not None else 0
       recovered.update(budget_timing_version=2,active_wall_seconds=active,queue_wait_seconds=max(0,elapsed-active))
      self._finish_job(j,recovered)
      j=self._job(job)
   if j.get('result_id'):return self.result(j['result_id'])
   return reply(s['run_id'],j['status'],job_id=job,candidate_digest=j['candidate_digest'],metrics={'depth':j['depth'],'track':j['track'],'resource_profile':j.get('resource_profile','legacy-single'),'progress':j.get('progress','waiting for host lock')})
  metrics={'registered_candidates':len(s['registered']),'completed_evaluations':len(s['results']),'budgets':s['budgets'],'budget_per_track':s['search_budget'],'latest_result_id':s.get('result_id'),'next_operations':['status','cancel'] if s['active_job'] else ['profile','baselines','register','evaluate','export'] if s['stage'] in ('public_search','public_ready') else []}
  controller=self._controller()
  if controller:
   metrics['controller']=controller.brief();action=metrics['controller']['next_action']['command']
   metrics['next_operations']={'archive':['export','artifact'],'research':['profile','baselines','recipe-create','register','evaluate'],
     'finish':['finish'],'wait':['status'],'status':['status','cancel'],'drain':['status'],'inspect_evidence':['artifact']}.get(action,[])
  return reply(s['run_id'],s['stage'],job_id=s['active_job'],candidate_digest=s.get('candidate_digest'),metrics=metrics)
 def cancel(self,job):
  j=self._job(job)
  if j['status'] not in ('queued','running'):return self.status(job=job)
  save(self.root/'jobs'/job/'cancel.json',{'requested_at':now()});return reply(j['run_id'],'cancellation_requested',job_id=job,candidate_digest=j['candidate_digest'])
 def _finish_job(self,j,raw):
  with lock(self.root/'jobs'/j['job_id']/'completion.lock'):
   return self._commit_job(j,raw)
 def _committed_job(self,job_id,state):
  # The ledger is authoritative for settlement; unrelated artifact health cannot
  # prevent charging and draining a new attempt. Legacy records remain readable.
  committed=state.get('job_results',{}).get(job_id)
  if committed:return committed
  if state['active_job']==job_id:return None
  for rid in reversed(state['results']):
   try:raw=self.raw(rid)
   except Exception:continue # A broken legacy artifact is not a settlement record.
   if raw['job_id']==job_id:return {'result_id':rid,'status':raw['status'],'completed_at':raw['completed_at']}
  return None
 def _commit_job(self,j,raw):
  s=self.state()
  committed=self._committed_job(j['job_id'],s)
  if committed:
   j.update(committed);save(self.root/'jobs'/j['job_id']/'job.json',j)
   return committed['result_id']
  if s['active_job']!=j['job_id']:raise Error('job_not_active')
  best=s.get('best_eligible_result')
  best_error=None
  try:best_cost=bridge.rank_cost(self.raw(best)) if best else float('inf')
  except Exception as error:best_cost=float('inf');best_error=getattr(error,'code','evidence_unavailable')
  s=self.state();raw={**raw,'schema_version':1,'run_id':s['run_id'],'job_id':j['job_id'],'candidate_digest':j['candidate_digest'],'card_digest':s['card_digest'],'runtime_digest':s['runtime_digest'],'track':j['track'],'depth':j['depth'],'workload':j.get('workload','independent_objects'),
   'resource_profile':j.get('resource_profile','legacy-single'),'resource_profile_digest':j.get('resource_profile_digest'),
   'resource_profile_details':j.get('resource_profile_details'),
   'resource_runtime_digest':j.get('resource_runtime_digest',s['runtime_digest']),
   'evaluation_mode':j.get('evaluation_mode','split'),'timing_scope':j.get('timing_scope','train-plus-development-v1'),'accounting_policy':j.get('accounting_policy','standalone-v1'),'completed_at':now()}
  raw['gate_digest']=digest({k:raw[k] for k in ('candidate_digest','card_digest','runtime_digest','resource_profile','resource_profile_digest','resource_runtime_digest','timing_scope','accounting_policy')});rid='r-'+digest(raw);dest=self.root/'results'/rid;dest.mkdir(exist_ok=True);save(dest/'raw.json',raw,0o444)
  ev=self.root/'jobs'/j['job_id']/'evidence'
  if ev.exists():
   for f in ev.iterdir():
    if f.is_file() and f.name!='raw.json':shutil.copyfile(f,dest/f.name)
  def change(s):
   if j['job_id']!=s['active_job']:return s
   s['active_job']=None;s.pop('pending_job',None);s['result_id']=rid;s['results'].append(rid)
   s.setdefault('job_results',{})[j['job_id']]={'result_id':rid,'status':raw['status'],'completed_at':raw['completed_at']}
   # The ledger's visibility index selects public identities without reading
   # hidden payloads. It is not qualification evidence; raw() still verifies it.
   s.setdefault('result_index',{})[rid]={'track':j['track'],'candidate_digest':j['candidate_digest'],
    'eligible':bool(raw.get('eligible')),'rank_cost':bridge.rank_cost(raw) if raw.get('eligible') else None,
    'primary':j.get('resource_profile','legacy-single')==j.get('primary_resource_profile','legacy-single')}
   if best_error:s['best_evidence_error']={'result_id':best,'reason_code':best_error}
   # Historical jobs retain their original wall-time charge. New jobs distinguish
   # shared-host queueing from active gate execution in their durable identity.
   charged=raw.get('active_wall_seconds',raw.get('wall_seconds',0)) if j.get('budget_timing_version')==2 else raw.get('wall_seconds',0)
   s['budgets'][j['track']]['wall_seconds']+=charged
   s['budgets'][j['track']]['queue_wait_seconds']=s['budgets'][j['track']].get('queue_wait_seconds',0)+raw.get('queue_wait_seconds',0)
   if raw.get('eligible'):
    s['stage']='public_ready'
    score=bridge.rank_cost(raw)
    # A one-worker reference is comparable evidence, but cannot silently become
    # the primary resource-profile winner.
    if j.get('resource_profile','legacy-single')==j.get('primary_resource_profile','legacy-single') and score<best_cost:s['best_eligible_candidate']=j['candidate_digest'];s['best_eligible_result']=rid
   return s
  self.ledger.update('evaluation_result',change);j.update(status=raw['status'],result_id=rid,completed_at=raw['completed_at']);save(self.root/'jobs'/j['job_id']/'job.json',j)
  controller=self._controller()
  if controller:controller.reconcile()
  return rid
 def raw(self,result):
  ident(result);s=self.state()
  if result not in s['results']:raise Error('result_not_committed')
  p=safe(self.root/'results'/result,'raw.json');raw=load(p,64*1048576)
  if 'r-'+digest(raw)!=result:raise Error('stale_result_digest')
  if any(raw.get(k)!=s[k] for k in ('run_id','card_digest','runtime_digest')) or raw.get('status') not in ('eligible','ineligible','failed','cancelled'):raise Error('result_context_mismatch')
  j=self._job(raw['job_id'])
  identity_keys=('resource_profile','resource_profile_digest','resource_profile_details','resource_runtime_digest','timing_scope','accounting_policy')
  # Pre-profile job snapshots have none of these fields.  Keep their historical
  # default identity readable, but reject every partial/new identity rather than
  # silently filling a missing current-profile field.
  if not any(k in j for k in identity_keys):
   identity={'resource_profile':'legacy-single','resource_profile_digest':None,'resource_profile_details':None,
             'resource_runtime_digest':raw['runtime_digest'],'timing_scope':'train-plus-development-v1','accounting_policy':'standalone-v1'}
  elif all(k in j for k in identity_keys):identity={k:j[k] for k in identity_keys}
  else:raise Error('result_job_mismatch')
  if any(raw.get(k)!=j.get(k) for k in ('run_id','candidate_digest','track','depth')) or raw.get('workload','independent_objects')!=j.get('workload','independent_objects') or any(raw.get(k,identity[k])!=identity[k] for k in identity_keys):raise Error('result_job_mismatch')
  return raw
 def result(self,result):
  r=self.raw(result);a=r.get('accounting',{});part=dataset.scored_partition(r);dev=a.get(part,{});fixed=r.get('fixed_costs',{});tim=r.get('timing',{})
  metrics={'result_id':result,'quality_passed':r.get('quality_passed',False),'eligible':r.get('eligible',False),'depth':r.get('depth'),'wall_seconds':r.get('wall_seconds'),
   'active_wall_seconds':r.get('active_wall_seconds'),'queue_wait_seconds':r.get('queue_wait_seconds'),
   'encode':tim.get('encode'),'decode':tim.get('decode'),'all_canonical_bytes':a.get('all',{}).get('canonical_bytes'),
   part+'_actual':dev.get('actual'),part+'_projection':dev.get('projection'),
   'deployment_horizon_bytes':dev.get('deployment_horizon_bytes'),'fixed_bytes':fixed.get('fixed_bytes'),'packed_source_bytes':fixed.get('packed_source_bytes'),'binary_bytes':fixed.get('binary_bytes'),'nonplatform_dependency_bytes':fixed.get('nonplatform_dependency_bytes')}
  if r.get('accounting_policy')=='supervisor-decoder-v1':
   metrics['decoder_accounting_'+part+'_actual']=bridge.decoder_accounting_metrics(r)
  metrics.update(bridge.result_metrics(r))
  from .screening import summary as screen_summary
  metrics['screening']=screen_summary(r);metrics['screen_passed']=r.get('screen_passed',False);metrics['deployment_views']=r.get('deployment_views')
  candidates=[]
  for rid in self.state()['results']:
   b=self.raw(rid)
   if b['track']=='baseline' and b.get('quality_passed') and b.get('depth')=='full' and bridge.comparison_identity(b)==bridge.comparison_identity(r):
    bd=b.get('accounting',{}).get(dataset.scored_partition(b),{});rate=bd.get('archive_ratio');speed=b.get('timing',{}).get(dataset.encoding_timing_key(b),{}).get('median_bytes_per_second')
    if rate is not None and speed is not None:
     bm,_=bridge.verify_candidate(self.root/'candidates'/b['candidate_digest']);candidates.append({'name':bm['candidate_id'],'result_id':rid,'archive_ratio':rate,'encode_bytes_per_second':speed,'deployment_total_bytes':bridge.rank_cost(b),'eligible':b.get('eligible',False)})
  matching=[b for b in candidates if dev.get('archive_ratio') is not None and b['archive_ratio']<=dev['archive_ratio']]
  metrics['baseline_comparison']={'best_size':min(candidates,key=lambda x:x['deployment_total_bytes']) if candidates else None,'best_eligible':min((b for b in candidates if b['eligible']),key=lambda x:x['deployment_total_bytes'],default=None),'fastest_matching_or_better_archive_ratio':max(matching,key=lambda x:x['encode_bytes_per_second'],default=None)}
  if r.get('error'):metrics['error']=r['error'][:600]
  return reply(r['run_id'],r['status'],job_id=r['job_id'],candidate_digest=r['candidate_digest'],reason_codes=r.get('reason_codes',[]),metrics=metrics,artifacts={'raw.json':{'sha256':sha(self.root/'results'/result/'raw.json'),'bytes':(self.root/'results'/result/'raw.json').stat().st_size}})
 def artifact(self,result,name='raw.json',offset=0,limit=16384):
  raw=self.raw(result)
  if name in bridge.EVIDENCE_NAMES:bridge.evidence_files(self.root/'results'/result,raw)
  if name not in ('raw.json','corruption-reproducer.hba','first-failure-input.hbi',*bridge.EVIDENCE_NAMES):raise Error('artifact_not_allowlisted')
  if not isinstance(offset,int) or not 0<=offset or not 1<=limit<=65536:raise Error('invalid_artifact_range')
  p=safe(self.root/'results'/result,name)
  with p.open('rb') as f:f.seek(offset);b=f.read(limit)
  return reply(self.state()['run_id'],metrics={'name':name,'sha256':sha(p),'bytes':p.stat().st_size,'offset':offset,'data_hex':b.hex() if name.endswith(('.hba','.hbi','.zip')) else None,'text':b.decode('utf-8','replace') if not name.endswith(('.hba','.hbi','.zip')) else None,'truncated':offset+len(b)<p.stat().st_size})
 def export(self,result,path):
  raw=self.raw(result);cid=raw['candidate_digest'];root=self.root/'candidates'/cid;m,r=bridge.verify_candidate(root);s=self.state();dataset.read_snapshot(self.root,s['card_digest'])
  _,pin=self._profile_context(raw.get('resource_profile'))
  if (raw['card_digest']!=s['card_digest'] or raw.get('resource_profile_digest')!=pin['resource_profile_digest']
      or raw.get('resource_profile_details')!=pin['resource_profile']
      or raw.get('resource_runtime_digest',raw['runtime_digest'])!=runner.require_fingerprint(pin['runtime'])['runtime_digest']):raise Error('stale_gate_digest')
  path=Path(path).absolute();path.parent.mkdir(parents=True,exist_ok=True)
  if path.exists():raise Error('export_exists')
  files={}
  for p in root.rglob('*'):
   if p.is_file():files['candidate/'+p.relative_to(root).as_posix()]=p
  for name,p in bridge.evidence_files(self.root/'results'/result,raw).items():files['evidence/'+name]=p
  files['evidence/raw.json']=self.root/'results'/result/'raw.json';files['dataset-card.json']=self.root/'dataset-card.json';files['runtime.json']=self.root/'runtime.json';files['runtime-profiles.json']=self.root/'runtime-profiles.json'
  from .instructions import verify_hypothesis
  if m.get('hypothesis',{}).get('experiment_id'):
   hypothesis=verify_hypothesis(self,m)
   files['evidence/hypothesis.json']=self.root/'hypotheses'/(hypothesis['experiment_id']+'.json')
  selected_runtime={'schema_version':1,'resource_profile':raw['resource_profile'],'resource_profile_digest':raw['resource_profile_digest'],
                    'resource_profile_details':raw['resource_profile_details'],'resource_runtime':pin['runtime'],
                    'resource_runtime_digest':raw['resource_runtime_digest'],'timing_scope':raw['timing_scope'],
                    'accounting_policy':raw['accounting_policy']}
  virtual={'selected-resource-runtime.json':canonical(selected_runtime)}
  decoder={'runtime_directory':'candidate/'+('decoder-runtime' if (root/'decoder-runtime').is_dir() else 'runtime'),
           'runtime_files':r.get('decoder_runtime_files',r.get('runtime_files')),
           'dependencies':r.get('decoder_dependencies',r.get('dependencies')),
           'included_hash_inventory_prefix':'candidate/'+('decoder-runtime/' if (root/'decoder-runtime').is_dir() else 'runtime/')}
  if m.get('schema_version')==2:
   runtime_root=root/'decoder-runtime';inventory=r['decoder_runtime_files'];hypothesis=m.get('hypothesis',{})
   if not runtime_root.is_dir():raise Error('missing_decoder_runtime')
   descriptor=load(safe(runtime_root,candidate.DECODER_COMMAND_FILE))
   if descriptor!=candidate.decoder_commands(m):raise Error('stale_decoder_command')
   command=[]
   for argument in descriptor['commands']['decode_stream']:
    command.append('./'+argument[len('{runtime}/'):] if argument.startswith('{runtime}/') else argument)
   command='COMPRESSION_LAB_THREADS='+str(pin['resource_profile']['threads'])+' LD_LIBRARY_PATH="$PWD/lib" '+shlex.join(command)+' < archive.hba > original.hbi'
   platform=[row for row in r['decoder_dependencies']['libraries'] if row['platform']]
   readme=("# Decoder-only bundle\n\n"
           "This archive contains only the independently staged decoder runtime and its non-platform libraries. It does not require the Compression Lab Python package.\n\n"
           "Extract it on a compatible Linux x86-64 host, then run from the extracted directory:\n\n"
           "```sh\n"+command+"\n```\n\n"
           "`archive.hba` must be the candidate's exact stream archive; `original.hbi` receives the decoded stream. The pinned codec is statically linked when the manifest declares `pinned-static-codec`; the dynamic loader, libc, libm, libstdc++, and libgcc remain platform dependencies and are documented in `DECODER_MANIFEST.json`, not redistributed here. Verify the hashes in that manifest before use.\n").encode()
   inventory_rows=[*inventory,{'path':'README.md','bytes':len(readme),'sha256':hashlib.sha256(readme).hexdigest()}]
   decoder_manifest={'schema_version':1,'role':'decoder-only-v2','candidate_digest':cid,'command':command,
                     'runtime_files':inventory_rows,'decoder_dependencies':r['decoder_dependencies'],
                     'platform_dependencies_not_included':platform,'static_linking':hypothesis.get('link_mode','not-declared'),
                     'platform_note':'Platform loader and libraries are recorded with hashes but are not shipped; all non-platform decoder dependencies are included under lib/.'}
   manifest_bytes=canonical(decoder_manifest)
   bundle=io.BytesIO()
   def write_member(archive,name,data,mode=0o444):
    info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.create_system=3;info.external_attr=(0o100000|mode)<<16
    archive.writestr(info,data,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
   with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
    for row in inventory:
     source=safe(runtime_root,row['path']);write_member(archive,row['path'],source.read_bytes(),source.stat().st_mode&0o777)
    write_member(archive,'README.md',readme);write_member(archive,'DECODER_MANIFEST.json',manifest_bytes)
   bundle_bytes=bundle.getvalue();virtual['decoder-bundle.zip']=bundle_bytes
   decoder.update({'format':'decoder-only-zip-v1','path':'decoder-bundle.zip','sha256':hashlib.sha256(bundle_bytes).hexdigest(),'bytes':len(bundle_bytes),
                   'decode_command':command,'manifest':'DECODER_MANIFEST.json','readme':'README.md','platform_dependencies_not_included':platform})
  inv={n:{'sha256':sha(p),'bytes':p.stat().st_size} for n,p in files.items()}
  inv.update({n:{'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)} for n,b in virtual.items()})
  with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
   for n,p in sorted(files.items()):z.write(p,n)
   for n,b in sorted(virtual.items()):z.writestr(n,b)
   z.writestr('EXPORT_MANIFEST.json',canonical({'schema_version':3,'candidate_digest':cid,'result_id':result,'files':inv,'decoder_bundle':decoder,'selected_resource_runtime':'selected-resource-runtime.json','private_data_included':False}))
  return reply(s['run_id'],'exported',candidate_digest=cid,metrics={'result_id':result,'path':str(path),'sha256':sha(path),'bytes':path.stat().st_size,'eligible':raw.get('eligible',False),'decoder_bundle':decoder})
 def baseline_table(self,resource_profile=None,candidate_result=None):
  if self.metadata()[0].get('workload')=='mutable_store':
   if resource_profile is not None or candidate_result is not None:raise Error('static_profile_on_mutable_workload')
   return bridge.baseline_table(self)
  from .screening import summary as screen_summary
  s=self.state();c,pin=self._profile_context(resource_profile);profile=c['_resource_profile']
  recipe_ids=(baselines.declared_morning_recipes() if c.get('resource_profiles') else list(baselines.recipes()))
  recipes=baselines.recipes();by_candidate={recipes[name].get('candidate_id',name):name for name in recipe_ids}
  common={'resource_profile':profile['id'],'resource_profile_digest':pin['resource_profile_digest'],
          'resource_runtime_digest':pin['runtime']['runtime_digest'],
          'timing_scope':c.get('timing_policy',{}).get('scope','train-plus-development-v1'),
          'accounting_policy':c.get('accounting_policy','standalone-v1'),'evaluation_mode':c.get('evaluation_mode','split'),'scored_partition':dataset.scored_partition(c),
          'primary_size_policy':c.get('primary_size_policy','strict-deployment-v1'),
          'encoding_floor_scope':c.get('timing_policy',{}).get('encoding_floor_scope','encode'),
          'resolved_cpus':profile['cpus'],'allowed_threads':profile['threads']}
  declared={name:{'name':name,'recipe_id':name,'status':'missing','state':'missing','result_ids':[],**common} for name in recipe_ids}
  expected=(s['card_digest'],s['runtime_digest'],c.get('workload','independent_objects'),'full',profile['id'],pin['resource_profile_digest'],pin['runtime']['runtime_digest'],common['timing_scope'],common['accounting_policy'])
  def same_profile_contract(raw):
   identity=bridge.comparison_identity(raw)
   return identity[:3]+identity[4:]==expected[:3]+expected[4:]
  def compact(raw,rid,name,manifest):
   part=dataset.scored_partition(raw);dev=raw.get('accounting',{}).get(part,{});tim=raw.get('timing',{});trials=[trial for phase in ('offline','encode','decode') for trial in raw.get('timing_trials',{}).get(phase,[])]
   encoding=tim.get(dataset.encoding_timing_key(c),{})
   native_rss=[x['observed_native_peak_rss_bytes'] for x in trials if x.get('observed_native_peak_rss_bytes') is not None]
   native_tasks=[x['max_observed_native_tasks'] for x in trials if x.get('max_observed_native_tasks') is not None]
   native_processes=[x['max_observed_native_processes'] for x in trials if x.get('max_observed_native_processes') is not None]
   decoder=bridge.decoder_accounting_metrics(raw);fixed=raw.get('fixed_costs',{});supervisor=raw.get('accounting_policy')=='supervisor-decoder-v1'
   metric_dev=raw.get('decoder_accounting',{}).get(part,{}) if supervisor else dev
   try:cost=bridge.rank_cost(raw)
   except Error:cost=None
   hypothesis=manifest.get('hypothesis',{}) if isinstance(manifest,dict) else {}
   return {'name':name,'result_id':rid,'candidate_digest':raw['candidate_digest'],'status':raw['status'],'state':'measured' if raw.get('quality_passed') else raw['status'],
    'depth':raw.get('depth'),'screening':screen_summary(raw),'quality_passed':raw.get('quality_passed',False),'eligible':raw.get('eligible',False),
    'reason_codes':raw.get('reason_codes',[]),'rank_cost_bytes':cost,'canonical_bytes':metric_dev.get('canonical_bytes'),part+'_archive_ratio':metric_dev.get('archive_ratio'),
    'bits_per_canonical_byte':metric_dev.get('bits_per_canonical_byte'),'compression_ratio':metric_dev.get('compression_ratio'),
    'archive_bytes':decoder['archive_bytes'] if supervisor else (dev.get('actual') or {}).get('archive_bytes'),
    'compiled_decoder_bytes':decoder['compiled_decoder_bytes'],'required_decoder_artifact_bytes':decoder['required_decoder_artifact_bytes'],
    'nonplatform_decoder_dependency_bytes':decoder['nonplatform_decoder_dependency_bytes'],'compressed_plus_decoder_bytes':decoder['compressed_plus_decoder_bytes'],
    'deployment_total_bytes':decoder['deployment_total_bytes'] if supervisor else cost,
    'primary_total_bytes':cost,'strict_decoder_deployment_total_bytes':decoder['deployment_total_bytes'],
    'excluded_standard_codec_bytes':raw.get('reported_accounting',{}).get(part,{}).get('excluded_standard_codec_bytes',0),
    'packed_source_bytes':fixed.get('packed_source_bytes'),'raw_source_config_bytes':fixed.get('raw_source_config_bytes'),
    'fixed_bytes':fixed.get('fixed_bytes'),'config_bytes':fixed.get('config_bytes'),'binary_bytes':fixed.get('binary_bytes'),
    'nonplatform_dependency_bytes':fixed.get('nonplatform_dependency_bytes'),
    'encoder_only_binary_bytes_reported_separately':fixed.get('encoder_only_binary_bytes_reported_separately'),
    'encoder_only_artifact_bytes_reported_separately':fixed.get('encoder_only_artifact_bytes_reported_separately'),
    'native_family':hypothesis.get('family'),'native_version':hypothesis.get('version'),'native_level':hypothesis.get('level'),
    'diagnostic_kind':raw.get('diagnostic_kind'),'declared_candidate_threads':raw.get('declared_candidate_threads',manifest.get('threads')),
    'implementation_declared_threads':raw.get('declared_candidate_threads',manifest.get('threads')),
    'encoding_floor_applied':raw.get('encoding_floor_applied'),
    **({'encoding_stages':bridge.result_metrics(raw)['encoding_stages']} if raw.get('timing_operation')=='offline-plus-online-v1' else {}),
    'encode_bytes_per_second':encoding.get('median_bytes_per_second'),'decode_bytes_per_second':tim.get('decode',{}).get('median_bytes_per_second'),
    'encode_MBps':encoding.get('median_decimal_MB_per_second'),'decode_MBps':tim.get('decode',{}).get('median_decimal_MB_per_second'),
    'observed_peak_rss_bytes':max(native_rss) if native_rss else None,'observed_peak_native_tasks':max(native_tasks) if native_tasks else None,
    'observed_peak_native_processes':max(native_processes) if native_processes else None,**common}
  measured=[]
  for rid in s['results']:
   raw=self.raw(rid)
   if raw.get('track')!='baseline' or not same_profile_contract(raw):continue
   manifest,_=bridge.verify_candidate(self.root/'candidates'/raw['candidate_digest']);name=by_candidate.get(manifest['candidate_id'])
   if name is None:continue
   row=compact(raw,rid,name,manifest);row['recipe_id']=name;row['result_ids']=[rid];measured.append(row)
   declared[name]['result_ids'].append(rid)
   if declared[name]['state']=='missing':declared[name].update(status=raw['status'],state='measured' if raw.get('quality_passed') else raw['status'])
  # Persisted factory failures have no candidate/result ID, but the declared
  # control must still be visible rather than disappearing from the table.
  receipts=sorted((self.root/'results').glob('preparation-*.json'))
  for receipt_path in receipts:
   receipt=load(receipt_path)
   if (receipt.get('resource_profile_digest')!=pin['resource_profile_digest'] or receipt.get('resource_runtime_digest')!=pin['runtime']['runtime_digest'] or receipt.get('timing_scope')!=common['timing_scope'] or receipt.get('accounting_policy')!=common['accounting_policy']):continue
   for failure in receipt.get('unavailable',[]):
    name=failure.get('name')
    if name in declared and declared[name]['state']=='missing':declared[name].update(status='failed',state='failed',reason_codes=[failure.get('reason_code')],error=failure.get('error'),preparation_receipt=receipt_path.name)
  for failure in s.get('profile_factory_failures',[]):
   if (failure.get('resource_profile')!=profile['id'] or failure.get('resource_profile_digest')!=pin['resource_profile_digest']
       or failure.get('resource_runtime_digest')!=pin['runtime']['runtime_digest']
       or failure.get('timing_scope')!=common['timing_scope'] or failure.get('accounting_policy')!=common['accounting_policy']):continue
   name=failure.get('recipe_id')
   if name in declared and declared[name]['state']=='missing':declared[name].update(status='failed',state='failed',reason_codes=[failure.get('reason_code')],error=failure.get('error'),failure_source='direct_factory')
  # Legacy workspaces predate profile-bound preparation receipts.  Their single
  # resource identity makes the old compatibility view unambiguous.
  unavailable=self.root/'baseline-unavailable.json'
  if not c.get('resource_profiles') and unavailable.exists():
   for failure in load(unavailable):
    name=failure.get('name')
    if name in declared and declared[name]['state']=='missing':declared[name].update(status='failed',state='failed',reason_codes=[failure.get('reason_code')],error=failure.get('error'))
  for job_dir in (self.root/'jobs').iterdir():
   job_file=job_dir/'job.json'
   if not job_file.is_file():continue
   job=load(job_file)
   if job.get('track')!='baseline' or job.get('status') not in ('queued','running') or job.get('resource_profile')!=profile['id'] or job.get('resource_profile_digest')!=pin['resource_profile_digest'] or job.get('resource_runtime_digest')!=pin['runtime']['runtime_digest'] or job.get('timing_scope')!=common['timing_scope'] or job.get('accounting_policy')!=common['accounting_policy']:continue
   manifest,_=bridge.verify_candidate(self.root/'candidates'/job['candidate_digest']);name=by_candidate.get(manifest['candidate_id'])
   if name in declared and declared[name]['state']=='missing':declared[name].update(status=job['status'],state=job['status'],job_id=job['job_id'])
  out=[row for row in declared.values() if not row['result_ids']]+measured
  if candidate_result is not None:
   raw=self.raw(candidate_result)
   if not same_profile_contract(raw):raise Error('incomparable_results','Candidate result has a different profile, timing scope, accounting policy, card, runtime or workload')
   matching=next((row for row in measured if row['result_id']==candidate_result),None)
   if matching is not None:matching['kind']='candidate'
   else:
    manifest,_=bridge.verify_candidate(self.root/'candidates'/raw['candidate_digest']);out.append({**compact(raw,candidate_result,manifest['candidate_id'],manifest),'kind':'candidate','result_ids':[candidate_result]})
  valid=[x for x in out if x.get('depth')=='full' and x.get('quality_passed') and x.get('rank_cost_bytes') is not None]
  def rank(rows,key,reverse=False,label='rank',gap='gap'):
   ranked=sorted((r for r in rows if r.get(key) is not None),key=lambda r:(r[key],r['name']) if not reverse else (-r[key],r['name']))
   if not ranked:return
   best=ranked[0][key]
   for i,row in enumerate(ranked,1):row[label]=i;row[gap]=(best-row[key]) if reverse else (row[key]-best)
  rank(valid,'rank_cost_bytes',label='size_rank',gap='size_gap_bytes')
  rank(valid,'encode_bytes_per_second',True,'encode_rank','encode_gap_bytes_per_second')
  rank(valid,'decode_bytes_per_second',True,'decode_rank','decode_gap_bytes_per_second')
  frontier=[]
  for row in valid:
   if row.get('encode_bytes_per_second') is None or row.get('decode_bytes_per_second') is None:continue
   dominated=any(other is not row and other.get('encode_bytes_per_second') is not None and other.get('decode_bytes_per_second') is not None and other['rank_cost_bytes']<=row['rank_cost_bytes'] and other['encode_bytes_per_second']>=row['encode_bytes_per_second'] and other['decode_bytes_per_second']>=row['decode_bytes_per_second'] and (other['rank_cost_bytes']<row['rank_cost_bytes'] or other['encode_bytes_per_second']>row['encode_bytes_per_second'] or other['decode_bytes_per_second']>row['decode_bytes_per_second']) for other in valid)
   if not dominated:frontier.append(row)
  return reply(s['run_id'],metrics={'rows':out,'pareto_frontier':frontier,'best_size':min(valid,key=lambda r:r['rank_cost_bytes']) if valid else None,'best_eligible':min((r for r in valid if r['eligible']),key=lambda r:r['rank_cost_bytes'],default=None),'resource_profile':profile,'timing_scope':common['timing_scope'],'accounting_policy':common['accounting_policy'],'runtime_digest':s['runtime_digest'],'resource_runtime_digest':pin['runtime']['runtime_digest'],'rank_policy':'Full, quality-passed rows in one card/runtime/profile/timing/accounting identity only. Size, encode and decode ranks are independent; screens have no ranks and valid slow controls remain listed.'})
 def inventory(self):
  from .inventory import inspect
  return reply(self.state()['run_id'],metrics=inspect())
 def brief(self):
  from .research import brief
  controller=self._controller()
  try:metrics=brief(self)
  except (OSError,ValueError,TypeError,KeyError,AttributeError) as error:
   if controller is None:raise
   metrics={'advisory_error':getattr(error,'code','invalid_advisory_evidence')}
  if controller:
   metrics['controller']=controller.brief();metrics['next_action']=metrics['controller']['next_action']
  return reply(self.state()['run_id'],metrics=metrics)
 def experiment_brief(self):
  controller=self._controller()
  if not controller:raise Error('controller_not_configured')
  return reply(self.state()['run_id'],metrics=controller.brief())
 def finish(self,request_id,candidate_digest=None,result_id=None,outcome='success'):
  controller=self._controller()
  if not controller:raise Error('controller_not_configured')
  receipt=controller.finish(request_id,candidate_digest,result_id,outcome)
  return reply(self.state()['run_id'],'finished' if receipt['accepted'] else 'rejected',metrics=receipt,reason_codes=receipt['reason_codes'])
 def lesson_propose(self,entry):
  from .refinement import RefinementStore
  controller=self._controller()
  if controller:controller.guard_refinement(mutation=True)
  return reply(self.state()['run_id'],metrics=RefinementStore(self).propose(entry))
 def lesson_check(self,lesson_id,result_ids):
  from .refinement import RefinementStore
  controller=self._controller()
  if controller:controller.guard_refinement(mutation=True)
  return reply(self.state()['run_id'],metrics=RefinementStore(self).check(lesson_id,result_ids))
 def lesson_list(self,limit=8):
  from .refinement import RefinementStore
  controller=self._controller()
  if controller:controller.guard_refinement()
  return reply(self.state()['run_id'],metrics=RefinementStore(self).list(limit))
 def feedback(self,result_id):
  from .research import feedback
  return reply(self.state()['run_id'],metrics=feedback(self,result_id))
 def _create_recipe_source(self,recipe_id,path,track):
  # Artifact training and factory failures are search work, not free preprocessing.
  with self._research_operation('factory',track):
   state=self.state();budget=state['budgets'].get(track,{'evaluations':0,'wall_seconds':0})
   if state['stage'] not in ('public_search','public_ready'):raise Error('terminal_latch')
   if state['active_job']:raise Error('job_active')
   if budget['evaluations']>=state['search_budget']['candidate_evaluations'] or budget['wall_seconds']>=state['search_budget']['wall_seconds']:raise Error('budget_exhausted')
   started=time.monotonic();failure=None
   try:
    card,rows=self.metadata();limits=self._search_limits(started,budget,card)
    runner.require_fingerprint(load(self.root/'runtime.json'),limits=limits)
    with lock(runner.HOST_LOCK,host=True,nonblocking=True):
     baselines.create(path,rows=rows,limits=limits,fitting_partition=dataset.fitting_partition(card),
       offline_replay=card.get('timing_policy',{}).get('operation')=='offline-plus-online-v1',
       **{k:v for k,v in baselines.recipes()[recipe_id].items() if k!='kind'})
     # Public templates must be readable across the evaluator/agent UID boundary.
     # A different agent UID copies into its own workbench/agent directory to edit.
     for file in Path(path).rglob('*'):
      if file.is_file():file.chmod(0o644)
   except BaseException as exc:
    failure={'recipe_id':recipe_id,'reason_code':getattr(exc,'code','factory_failed'),'error':str(exc)[:2000]}
    raise
   finally:
    elapsed=time.monotonic()-started
    def charge(state):
     budget=state['budgets'].setdefault(track,{'evaluations':0,'wall_seconds':0});budget['wall_seconds']+=elapsed
     if state.get('active_operation'):state['active_operation'].update(charged=True,charged_wall_seconds=elapsed)
     if failure:
      if failure['reason_code']!='busy':budget['evaluations']+=1
      state.setdefault('factory_failures',[]).append({'track':track,'wall_seconds':elapsed,**failure})
     return state
    self.ledger.update('factory_attempt',charge)
 def create_recipe(self,recipe_id,name):
  if self.metadata()[0].get('implementation_policy')=='from_scratch':raise Error('from_scratch_recipe_forbidden')
  if self.metadata()[0].get('workload')=='mutable_store':raise Error('static_recipe_on_mutable_workload')
  if recipe_id not in baselines.recipes():raise Error('unknown_baseline_recipe')
  ident(name)
  if self.state()['stage'] not in ('public_search','public_ready'):raise Error('terminal_latch')
  parent=self.root/'workbench'
  if parent.is_symlink() or not parent.is_dir():raise Error('unsafe_workbench')
  path=parent/name
  if path.exists() or path.is_symlink():raise Error('candidate_path_exists')
  self._create_recipe_source(recipe_id,path,'agent')
  return reply(self.state()['run_id'],metrics={'recipe_id':recipe_id,'candidate_path':str(path),'registered':False,'warning':'Public source template. Different agent UID: copy into an agent-owned directory before editing. Registration snapshots bytes; edits are not exact factory provenance.'})
 def baseline_trial(self,recipe_id,depth='screen',wait=False,resource_profile=None):
  if depth not in ('screen','quick','full'):raise Error('invalid_evaluation_mode')
  c,pin=self._profile_context(resource_profile);_,rows=self.metadata();s=self.state()
  if c.get('workload')=='mutable_store':raise Error('static_recipe_on_mutable_workload')
  recipe=baselines.recipes().get(recipe_id)
  if recipe is None:raise Error('unknown_baseline_recipe')
  runner.require_fingerprint(pin['runtime'])
  parent=self.root/'workbench'
  if parent.is_symlink() or not parent.is_dir():raise Error('unsafe_workbench')
  # Generate the requested factory content every time. Named mutable workbenches
  # and human-readable candidate IDs are deliberately not trusted as cache keys.
  with tempfile.TemporaryDirectory(prefix='.factory-',dir=parent) as td:
   source=Path(td)/'source'
   try:self._create_recipe_source(recipe_id,source,'baseline')
   except Error as exc:
    if exc.code!='busy':
     failure={'recipe_id':recipe_id,'reason_code':exc.code,'error':str(exc)[:2000],
              'resource_profile':c['_resource_profile']['id'],'resource_profile_digest':pin['resource_profile_digest'],
              'resource_runtime_digest':pin['runtime']['runtime_digest'],
              'timing_scope':c.get('timing_policy',{}).get('scope','train-plus-development-v1'),
              'accounting_policy':c.get('accounting_policy','standalone-v1')}
     self.ledger.update('profile_factory_failure',lambda state:{**state,'profile_factory_failures':[*(state.get('profile_factory_failures',[])),failure]})
    raise
   reg=self.register(source,'baseline')
  cid=reg['candidate_digest'];s=self.state()
  for rid in reversed(s['results']):
   raw=self.raw(rid)
   if (raw.get('track')=='baseline' and raw['candidate_digest']==cid and raw['depth']==depth
       and raw['card_digest']==s['card_digest'] and raw['runtime_digest']==s['runtime_digest']
       and raw.get('workload','independent_objects')==c.get('workload','independent_objects')
       and raw.get('resource_profile','legacy-single')==c['_resource_profile']['id']
       and raw.get('resource_profile_digest')==pin['resource_profile_digest']
       and raw.get('resource_runtime_digest',raw['runtime_digest'])==pin['runtime']['runtime_digest']
       and raw.get('timing_scope','train-plus-development-v1')==c.get('timing_policy',{}).get('scope','train-plus-development-v1')
       and raw.get('accounting_policy','standalone-v1')==c.get('accounting_policy','standalone-v1')):
    result=self.result(rid);result['metrics']['exact_recipe_cache_hit']=True;return result
  return self.evaluate(cid,depth,'baseline',wait,resource_profile=c['_resource_profile']['id'])
 def prepare_baselines(self,matrix=None,depth='full',resource_profile=None):
  if self.metadata()[0].get('workload')=='mutable_store':
   if resource_profile is not None:raise Error('static_profile_on_mutable_workload')
   return bridge.prepare_baselines(self,depth)
  c,pin=self._profile_context(resource_profile)
  matrix=(baselines.declared_morning_recipes() if matrix is None and c.get('resource_profiles') else baselines.MATRIX if matrix is None else matrix);done=[];fail=[];recipe_ids=[]
  for item in matrix:
   if isinstance(item,str):name=item
   else:
    family,level,dictionary=item;name=family+(('-'+str(level)) if family!='stored' else '')+('-dict' if dictionary else '')
   recipe_ids.append(name)
   try:done.append(self.baseline_trial(name,depth,True,c['_resource_profile']['id']))
   except Error as e:fail.append({'name':name,'status':'unavailable','reason_code':e.code,'error':str(e)})
  # The latest convenience view is mutable; append-only preparation receipts are
  # separate. Evaluation results and build failures remain in the durable ledger.
  receipt={'schema_version':1,'created_at':now(),'depth':depth,'recipes':recipe_ids,'resource_profile':c['_resource_profile'],'resource_profile_digest':pin['resource_profile_digest'],'resource_runtime_digest':pin['runtime']['runtime_digest'],'timing_scope':c.get('timing_policy',{}).get('scope','train-plus-development-v1'),'accounting_policy':c.get('accounting_policy','standalone-v1'),'unavailable':fail,
           'results':[x.get('metrics',{}).get('result_id') for x in done]}
  receipt_path=self.root/'results'/('preparation-'+digest(receipt)+'.json')
  save(receipt_path,receipt,0o444);save(self.root/'baseline-unavailable.json',fail)
  table=self.baseline_table(c['_resource_profile']['id']);table['metrics'].update(unavailable=fail,preparation_receipt=str(receipt_path),
    cache_policy='Exact factory bytes/build, card, runtime, workload and depth; failures retained')
  return table
 def compare(self,result_ids,diagnostics=False):return bridge.compare(self,result_ids,diagnostics)
 def resume(self):
  controller=self._controller()
  if controller:
   if self.state()['active_job']:self.status(job=self.state()['active_job'])
   return reply(self.state()['run_id'],metrics=controller.reconcile())
  return bridge.resume(self)
 def control(self,method='deterministic',evaluations=3,seed=20260906):
  self._guard_research()
  if self.metadata()[0].get('workload')=='mutable_store':raise Error('mutable_settings_control_unsupported','Use the durable reference baseline, not static settings search')
  s=self.state()
  if method not in ('deterministic','random') or not 0<evaluations<=s['search_budget']['candidate_evaluations']:raise Error('invalid_control_budget')
  choices=list(baselines.MATRIX)
  if method=='random':random.Random(seed).shuffle(choices)
  results=[];start=time.monotonic()
  for i in range(evaluations):
   if time.monotonic()-start>=s['search_budget']['wall_seconds']:break
   family,level,dictionary=choices[i%len(choices)];path=self.root/'workbench'/f'control-{method}-{i}-{uuid.uuid4().hex[:8]}'
   recipe=family+(('-'+str(level)) if family!='stored' else '')+('-dict' if dictionary else '')
   try:
    self._create_recipe_source(recipe,path,method);r=self.register(path,method);results.append(self.evaluate(r['candidate_digest'],'full',method,True))
   except Error as e:results.append({'status':'failed','reason_codes':[e.code],'error':str(e)})
  result={'method':method,'seed':seed,'budget':s['search_budget'],'requested_evaluations':evaluations,'elapsed_seconds':time.monotonic()-start,'results':results,'attribution':'This control searches conventional settings; gains from selecting these settings are not novel algorithms.'};save(self.root/(method+'-control.json'),result)
  return reply(s['run_id'],metrics=result)

class ResearcherEngine(Engine):
 """Public transport view; Engine remains the trusted owner/worker API.

 Hidden runs select identities from the host ledger before reading evidence.
 The evaluator workspace must also be outside the researcher's filesystem
 namespace: a Python view is not a filesystem authorization boundary.
 """
 def _hidden(self):
  return Engine(self.root).metadata()[0].get('baseline_visibility','visible')=='hidden'
 @staticmethod
 def _public_track(track):return track in ('agent','deterministic','random')
 def state(self):
  state=super().state()
  card,_=dataset.read_metadata(self.root,state['card_digest'])
  if card.get('baseline_visibility','visible')!='hidden':return state
  owner=Engine(self.root);index=state.get('result_index',{})
  results=[rid for rid in state['results'] if self._public_track(index.get(rid,{}).get('track'))]
  registrations=[r for r in state.get('registrations',[]) if self._public_track(r['track'])]
  # Explicit state schema, never a recursive key/string scrub of owner data.
  public={key:state[key] for key in ('stage','run_id','card_digest','runtime_digest',
    'runtime_profiles_digest','mode','created_at','search_budget') if key in state}
  public.update(results=results,registered=sorted({r['candidate_digest'] for r in registrations}),
   budgets={track:value for track,value in state['budgets'].items() if self._public_track(track)},active_job=None)
  if registrations:public['candidate_digest']=registrations[-1]['candidate_digest']
  if results:public['result_id']=results[-1]
  best=min((rid for rid in results if index[rid]['eligible'] and index[rid]['primary']),
   key=lambda rid:index[rid]['rank_cost'],default=None)
  if best:public.update(best_eligible_result=best,best_eligible_candidate=index[best]['candidate_digest'])
  if public['stage'] in ('public_search','public_ready'):
   public['stage']='public_ready' if any(index[rid]['eligible'] for rid in results) else 'public_search'
  if state['active_job']:
   job=owner._job(state['active_job'])
   if self._public_track(job['track']):
    public.update(active_job=job['job_id'],candidate_digest=job['candidate_digest'])
    if state.get('pending_job'):public['pending_job']=state['pending_job']
  operation=state.get('active_operation')
  if operation and self._public_track(operation['track']):public['active_operation']=operation
  public['evaluation_requests']={request:record for request,record in state.get('evaluation_requests',{}).items()
   if self._public_track(owner._job(record['job_id'])['track'])}
  public['job_results']={job:record for job,record in state.get('job_results',{}).items() if record['result_id'] in results}
  return public
 def raw(self,result):
  if self._hidden():
   ident(result);entry=super().state().get('result_index',{}).get(result,{})
   if not self._public_track(entry.get('track')):raise Error('result_not_public')
  raw=Engine(self.root).raw(result)
  if self._hidden() and not self._public_track(raw.get('track')):raise Error('result_not_public')
  return raw
 def _job(self,job):
  value=Engine(self.root)._job(job)
  if self._hidden() and not self._public_track(value.get('track')):raise Error('job_not_public')
  return value
 def result(self,result):
  value=super().result(result)
  if self._hidden():
   value['metrics'].pop('baseline_comparison',None)
   value['metrics'].update(baseline_visibility='hidden',supplied_comparison_available=False)
  return value
 def _check_track(self,track):
  if self._hidden() and not self._public_track(track):raise Error('supplied_baseline_unavailable')
 def register(self,path,track='agent'):
  self._check_track(track)
  return super().register(path,track)
 def evaluate(self,cid,depth='full',track='agent',wait=False,workload=None,resource_profile=None,request_id=None):
  self._check_track(track)
  if self._hidden() and cid not in self.state()['registered']:raise Error('candidate_not_registered')
  return super().evaluate(cid,depth,track,wait,workload,resource_profile,request_id)
 def _no_supplied_comparison(self):
  return reply(self.state()['run_id'],metrics={'baseline_visibility':'hidden',
   'supplied_comparison_available':False,'rows':[],
   'notice':'No supplied baseline comparison is available for this run. Follow brief for the implementation condition; register and evaluate your own experiments.'})
 def baseline_table(self,resource_profile=None,candidate_result=None):
  if self._hidden():return self._no_supplied_comparison()
  return super().baseline_table(resource_profile,candidate_result)
 def baseline_trial(self,recipe_id,depth='screen',wait=False,resource_profile=None):
  if self._hidden():return self._no_supplied_comparison()
  if self.metadata()[0].get('implementation_policy')=='from_scratch':raise Error('from_scratch_recipe_forbidden')
  return super().baseline_trial(recipe_id,depth,wait,resource_profile)
 def prepare_baselines(self,matrix=None,depth='full',resource_profile=None):
  if self._hidden():return self._no_supplied_comparison()
  if self.metadata()[0].get('implementation_policy')=='from_scratch':raise Error('from_scratch_recipe_forbidden')
  return super().prepare_baselines(matrix,depth,resource_profile)


def doctor(release=False,owner=None):
 tools=runner.fingerprint();sandbox=runner.probe();reasons=[]
 if sandbox['status']!='available':reasons+=['sandbox_unavailable']
 if 'g++' not in tools['tools']:reasons+=['blocked_toolchain']
 if release:
  if tools['effective_cgroup_limits']['status']!='verified':reasons+=['effective_cgroup_limits_unavailable']
  if os.geteuid()==0:reasons+=['root_build_process_limit_not_certified']
  if owner is None:reasons+=['owner_boundary_missing']
  else:
   try:
    from .owner import boundary
    boundary(Path(owner))
   except Exception as e:reasons+=[getattr(e,'code','owner_boundary_unverified')]
 try:sdk=importlib.metadata.version('mcp')
 except importlib.metadata.PackageNotFoundError:sdk=None
 return reply(status='blocked' if reasons else 'ok',reason_codes=reasons,metrics={'certification_check':release,'runtime':tools,'sandbox':sandbox,'mcp_sdk_installed':sdk,'mcp_note':'Optional; protocol tests require official mcp==1.29.1. No client/model call is made.','installed_native_libraries':{k:str(v) for k,v in candidate.libraries().items() if k in ('libzstd.so.1','liblz4.so.1','libbrotlienc.so.1','liblzma.so.5')}})
