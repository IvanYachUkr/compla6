"""Disposable durable job worker; no daemon or model orchestrator."""
import os,signal,sys,time
from pathlib import Path
from .engine import Engine
from .workloads.bridge import evaluate
from .util import Error,save,lock,load
from . import runner

def run(root,job):
 e=Engine(root);j=e._job(job);jd=e.root/'jobs'/job;cancel=jd/'cancel.json';started=time.monotonic();active_started=None
 controller=e._controller()
 deadline_epoch=controller.state()['protocol']['deadline_epoch'] if controller else None
 def stop(signum,frame):save(cancel,{'signal':signum})
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 j.update(pid=os.getpid(),proc_start=Path(f'/proc/{os.getpid()}/stat').read_text().split()[21]);save(jd/'job.json',j)
 try:
  while True:
   if cancel.exists():raise Error('cancelled')
   if deadline_epoch is not None and time.time()>=deadline_epoch:raise Error('controller_deadline')
   try:
    with lock(runner.HOST_LOCK,nonblocking=True,host=True):
     if controller:controller.guard_research()
     active_started=time.monotonic()
     j.update(active_started_epoch=time.time(),status='running',progress='loading and verifying public inputs')
     save(jd/'job.json',j)
     s=e.state();c,pin=e._profile_context(j.get('resource_profile'))
     if j.get('resource_profile_digest')!=pin['resource_profile_digest'] or j.get('resource_profile_details')!=pin['resource_profile'] or j.get('resource_runtime_digest')!=pin['runtime']['runtime_digest']:
      raise Error('stale_resource_profile_fingerprint')
     if j.get('timing_scope','train-plus-development-v1')!=c.get('timing_policy',{}).get('scope','train-plus-development-v1') or j.get('accounting_policy','standalone-v1')!=c.get('accounting_policy','standalone-v1'):
      raise Error('stale_evaluation_policy')
     # The primary fingerprint stays in runtime.json for workspace-wide state.
     # Each queued profile has its own sealed expected allocation and runtime.
     if runner.require_fingerprint(pin['runtime'])['runtime_digest']!=j['resource_runtime_digest']:raise Error('stale_runtime_digest')
     _,metadata_rows=e.metadata()
     rows=metadata_rows if j['depth']=='screen' else e.data()[1]
     j.update(status='running',progress='correctness, native diagnostics, accounting, fresh-process timing');save(jd/'job.json',j)
     budget_start=active_started if j.get('budget_timing_version')==2 else started
     deadline=budget_start+max(0,s['search_budget']['wall_seconds']-s['budgets'][j['track']]['wall_seconds'])
     if deadline_epoch is not None:deadline=min(deadline,time.monotonic()+max(0,deadline_epoch-time.time()))
     raw=evaluate(e.root/'candidates'/j['candidate_digest'],rows,c,jd/'evidence',j['depth'],s['mode'],cancel,deadline,j.get('workload'))
     if runner.require_fingerprint(pin['runtime'])['runtime_digest']!=j['resource_runtime_digest']:raw.update(status='failed',quality_passed=False,eligible=False,reason_codes=['runtime_changed_during_evaluation'])
     break
   except Error as ex:
    if ex.code!='busy':raise
    if time.monotonic()-started>e.state()['search_budget']['wall_seconds']:raise Error('host_lock_wait_budget_exhausted')
    time.sleep(.1)
 except BaseException as ex:
  raw={'status':'cancelled' if getattr(ex,'code',None)=='cancelled' else 'failed','quality_passed':False,'eligible':False,'reason_codes':[getattr(ex,'code','worker_error')],'error':str(ex)[:2000],'wall_seconds':time.monotonic()-started}
 raw['gate_wall_seconds']=raw.get('wall_seconds');raw['wall_seconds']=time.monotonic()-started
 if j.get('budget_timing_version')==2:
  raw['budget_timing_version']=2
  raw['queue_wait_seconds']=max(0,j.get('active_started_epoch',time.time())-j['created_epoch'])
  raw['active_wall_seconds']=time.monotonic()-active_started if active_started is not None else 0
  raw['wall_seconds']=raw['queue_wait_seconds']+raw['active_wall_seconds']
 e._finish_job(j,raw)
def main():run(Path(sys.argv[1]),sys.argv[2])
if __name__=='__main__':main()
