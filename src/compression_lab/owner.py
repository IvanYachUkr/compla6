"""Owner-only private evaluation. OS identity/permissions, not MCP, is the boundary.

Public evidence is untrusted: freeze copies through no-follow descriptors and
reruns public gates under the owner. Private object bytes are first opened only
after private_started has been fsync'd into the owner journal.
"""
from __future__ import annotations
import json,os,pwd,grp,stat,uuid
from pathlib import Path
from .util import Error,Ledger,load,save,atomic,secure_read,secure_directory,secure_names,sha,digest,lock,now,reply,rel
from . import candidate,dataset,runner
from .workloads import bridge,registry,cards

def primary_resources(card):
 selected=dataset.select_profile(card)['_resource_profile']
 return {key:selected[key] for key in ('id','threads','cpus')}

def runtime_fingerprint(policy):
 # Schema 1 intentionally retains its historical default-one-thread identity.
 # Never reinterpret an already recorded owner result using a newer policy.
 if policy.get('schema_version')==1:return runner.fingerprint()
 if policy.get('schema_version')!=2:raise Error('invalid_owner_runtime_policy')
 resource=policy.get('resource_profile')
 if not isinstance(resource,dict):raise Error('invalid_owner_resource_profile')
 dataset.validate_profiles({'schema_version':1,'primary':resource.get('id'),'profiles':[resource]})
 return runner.fingerprint(threads=resource['threads'],cpus=resource['cpus'])

def require_primary_resources(policy,card):
 if policy.get('schema_version')==2 and primary_resources(card)!=policy['resource_profile']:raise Error('owner_resource_profile_changed')

def boundary(root,release=True):
 root=Path(root).absolute();st=root.lstat()
 if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o700:raise Error('owner_directory_not_private','Owner directory must be owned by evaluator UID, mode 0700, and not a symlink')
 for p in [root,*root.parents]:
  if p.is_symlink():raise Error('owner_symlink_ancestor')
 if any(root==Path(p) or Path(p) in root.parents for p in ('/usr','/lib','/lib64','/bin','/sbin')):raise Error('private_data_in_toolchain_mount')
 try:
  if 'system.posix_acl_access' in os.listxattr(root):raise Error('owner_acl_not_supported','Remove named ACLs; v1 proves simple POSIX permissions only')
 except AttributeError:raise Error('acl_inspection_unavailable')
 secure_names(root,owner_uid=os.geteuid(),max_files=None,max_bytes=None)
 p=load(root/'owner-policy.json');uid=p['agent_uid']
 if p['owner_uid']!=os.geteuid() or uid in (0,os.geteuid()):raise Error('agent_owner_identity_not_separate')
 try:
  user=pwd.getpwuid(uid);groups=os.getgrouplist(user.pw_name,user.pw_gid)
  bad={grp.getgrgid(g).gr_name for g in groups}&{'sudo','wheel','docker','lxd','incus-admin'}
  if bad:raise Error('agent_privileged_group',','.join(sorted(bad)))
 except KeyError:raise Error('agent_uid_not_configured','Use a real dedicated unprivileged OS account')
 if release:
  if not p.get('benchmark_host'):raise Error('benchmark_host_not_designated')
  lockpath=Path(runner.HOST_LOCK)
  if not lockpath.is_file() or lockpath.is_symlink() or lockpath.stat().st_uid!=0 or lockpath.stat().st_nlink!=1:raise Error('host_lock_not_admin_owned','Provision /run/compression-lab/benchmark.lock, root-owned mode 0666')
  if os.geteuid()==0:raise Error('root_build_process_limit_not_certified','Use a separate non-root evaluator account; build RLIMIT_NPROC is not a root boundary')
  runner.probe.cache_clear()
  if runner.probe()['status']!='available':raise Error('sandbox_unavailable')
  if runtime_fingerprint(p)['runtime_digest']!=p['runtime_digest']:raise Error('owner_runtime_changed')
 return {'owner_uid':p['owner_uid'],'agent_uid':uid,'filesystem_mode':'0700','root':str(root),'benchmark_host':p.get('benchmark_host',False),'policy':p}

def init(root,agent_uid,private_dataset,workspace,benchmark_host=False):
 root=Path(root).absolute()
 with secure_directory(root.parent) as parent:
  try:os.mkdir(root.name,0o700,dir_fd=parent)
  except FileExistsError:pass
 with secure_directory(root) as fd:
  if os.fstat(fd).st_uid!=os.geteuid():raise Error('owner_directory_not_private')
  os.fchmod(fd,0o700)
 secure_names(root,owner_uid=os.geteuid(),max_files=None,max_bytes=None)
 if (root/'owner-policy.json').exists():raise Error('owner_already_initialized')
 private_dataset=Path(private_dataset).absolute()
 try:relative=private_dataset.relative_to(root).as_posix()
 except ValueError:raise Error('private_dataset_must_be_inside_owner_root')
 rel(relative)
 # Only path/permissions metadata is inspected here, not hidden bytes.
 if not private_dataset.is_file() or private_dataset.is_symlink():raise Error('private_card_missing_or_symlink')
 from .engine import Engine
 e=Engine(workspace);s=e.state();c,_=e.metadata()
 if c.get('evaluation_mode')=='whole_dataset':raise Error('whole_dataset_has_no_private_test','Use the public whole-corpus result and export; this experiment has no held-out partition')
 policy={'schema_version':2,'owner_uid':os.geteuid(),'agent_uid':agent_uid,'private_card':relative,'public_workspace':str(e.root),'resource_profile':primary_resources(c),'benchmark_host':benchmark_host,'created_at':now(),'threat_model':'Dedicated agent UID has no sudo/capabilities/privileged group, and cannot acquire evaluator UID. Kernel/toolchain/evaluator are trusted.'}
 policy['runtime_digest']=runtime_fingerprint(policy)['runtime_digest']
 save(root/'owner-policy.json',policy);boundary(root,False)
 l=Ledger(root/'state');l.create({'stage':'created','run_id':'private-'+uuid.uuid4().hex,'card_digest':s['card_digest'],'runtime_digest':policy['runtime_digest'],'candidate_digest':None,'private_attempts':0,'disclosed':False});l.transition('public_search')
 return reply(l.read()['run_id'],'public_search',metrics={'boundary':'Separate UID + owner-only 0700 tree','private_bytes_read':False,'benchmark_host_designated':benchmark_host,'next_owner_operation':'freeze'})

def copy_candidate(src,dst,cid):
 from .util import pairs
 r=json.loads(secure_read(src,'registration.json',16*1048576),object_pairs_hook=pairs)
 if r.get('candidate_digest')!=cid or digest({k:v for k,v in r.items() if k!='candidate_digest'})!=cid:raise Error('untrusted_candidate_digest')
 names=['registration.json','source.zip']+['source/'+x['path'] for x in r['source_files']]+['runtime/'+x['path'] for x in r['runtime_files']]
 if 'decoder_runtime_files' in r:names+=['decoder-runtime/'+x['path'] for x in r['decoder_runtime_files']]
 dst.mkdir(parents=True,exist_ok=False)
 for name in names:
  b=secure_read(src,name);target=dst/name;atomic(target,b,0o444)
 if r.get('workload')=='mutable_store':
  m=registry.validate(load(dst/'source'/registry.MANIFEST))
  executables=[m['python_runtime']['executable']] if m['language']=='python' else [registry.native_manifest(m)['executable']]
 else:
  m=candidate.manifest(load(dst/'source/candidate.json'));executables=[m['executable']]
  if m['schema_version']==2:executables.append(candidate.decoder_role(m)['executable'])
 for name in executables:(dst/'runtime'/rel(name)).chmod(0o555)
 if 'decoder_runtime_files' in r:(dst/'decoder-runtime'/rel(candidate.decoder_role(m)['executable'])).chmod(0o555)
 bridge.verify_candidate(dst)

def copy_public(src,dst,expected):
 from .util import pairs
 card=json.loads(secure_read(src,'dataset-card.json'),object_pairs_hook=pairs);manifest=json.loads(secure_read(src,card['public_manifest']),object_pairs_hook=pairs)
 if digest({'card':card,'manifest':manifest})!=expected:raise Error('public_card_digest_mismatch')
 dst.mkdir(parents=True,exist_ok=False);atomic(dst/'dataset-card.json',secure_read(src,'dataset-card.json'),0o444);atomic(dst/card['public_manifest'],secure_read(src,card['public_manifest']),0o444)
 for r in manifest['objects']:atomic(dst/'public'/r['path'],secure_read(src,'public/'+r['path']),0o444)
 dataset.read_snapshot(dst,expected)

def freeze(root,cid):
 root=Path(root).absolute();b=boundary(root);p=b['policy'];l=Ledger(root/'state')
 with lock(root/'owner-operation.lock',nonblocking=True),lock(runner.HOST_LOCK,host=True):
  s=l.read()
  if s['stage'] not in ('public_search','public_ready'):raise Error('freeze_not_allowed')
  from .engine import Engine
  public=Engine(p['public_workspace']);ps=public.state()
  if ps['active_job']:raise Error('public_job_active')
  if cid not in ps['registered']:raise Error('candidate_not_registered')
  dest=root/'candidates'/cid
  if not dest.exists():copy_candidate(public.root/'candidates'/cid,dest,cid)
  else:bridge.verify_candidate(dest)
  publiccopy=root/('public-'+ps['card_digest'])
  if not publiccopy.exists():copy_public(public.root,publiccopy,ps['card_digest'])
  c,rows=dataset.read_snapshot(publiccopy,ps['card_digest'])
  require_primary_resources(p,c)
  # Smoke cards cannot be relabelled retroactively by the owner.
  if c.get('timing_policy',{}).get('role','smoke')!='benchmark':raise Error('smoke_not_certified')
  train_hashes=[r['canonical_sha256'] for r in rows if r['split']=='train']
  attempt=root/'public-rechecks'/uuid.uuid4().hex;r=bridge.evaluate(dest,rows,c,attempt,'full','required',train_hashes=train_hashes)
  if not r.get('eligible'):return reply(s['run_id'],'blocked',candidate_digest=cid,reason_codes=r.get('reason_codes',[]),metrics={'private_bytes_read':False,'public_recheck_path':str(attempt)})
  l.transition('public_ready',candidate_digest=cid,card_digest=ps['card_digest']);save(root/'frozen-card.json',c,0o444)
  s=l.transition('frozen',candidate_digest=cid,freeze_digest=digest({'candidate':cid,'card':ps['card_digest'],'runtime':p['runtime_digest']}),frozen_at=now(),public_snapshot=publiccopy.name,training_object_sha256=train_hashes)
  return reply(s['run_id'],'frozen',candidate_digest=cid,metrics={'freeze_digest':s['freeze_digest'],'private_bytes_read':False,'public_recheck_path':str(attempt)})

def private_rows(root,policy,card):
 from .util import pairs
 pc=json.loads(secure_read(root,policy['private_card']),object_pairs_hook=pairs)
 if set(pc)!={'schema_version','adapter','private_manifest'} or pc['schema_version']!=1 or pc['adapter']!=card['adapter']:raise Error('invalid_private_card')
 base=Path(policy['private_card']).parent;mp=base/rel(pc['private_manifest']);m=json.loads(secure_read(root,mp.as_posix()),object_pairs_hook=pairs)
 if m.get('schema_version')!=1 or set(m)-{'schema_version','objects','scope'}:raise Error('invalid_private_manifest')
 from .stream import ALIAS_RE
 from .hcb import decode_hcb
 import hashlib
 rows=[];seen=set();snapshot=root/'private-snapshot';snapshot.mkdir(exist_ok=True)
 for i,r in enumerate(m['objects']):
  if set(r)-{'alias','group','source_group','split','path','canonical_bytes','canonical_sha256'}:raise Error('invalid_private_object')
  alias=r.get('alias','');group=r.get('group') or r.get('source_group')
  if not ALIAS_RE.fullmatch(alias) or alias in seen or not isinstance(group,str) or not group:raise Error('invalid_private_alias_group')
  seen.add(alias);data=secure_read(root,(mp.parent/rel(r['path'])).as_posix())
  if len(data)!=r['canonical_bytes'] or hashlib.sha256(data).hexdigest()!=r['canonical_sha256']:raise Error('private_input_digest_mismatch')
  if card['adapter']=='hcb1':decode_hcb(data)
  elif card['adapter']=='relational_bundle':cards.validate_bundle(card,data)
  out=snapshot/(r['canonical_sha256']+'.bin')
  if not out.exists():atomic(out,data,0o400)
  rows.append({'alias':alias,'group':group,'split':'development','canonical_bytes':len(data),'canonical_sha256':r['canonical_sha256'],'source':out})
 if not rows:raise Error('empty_private_partition')
 return rows

def evaluate(root,resume=False):
 root=Path(root).absolute();b=boundary(root);policy=b['policy'];l=Ledger(root/'state')
 with lock(root/'owner-operation.lock',nonblocking=True),lock(runner.HOST_LOCK,host=True):
  s=l.read()
  if s['stage']=='private_started':
   if not resume:raise Error('resume_required','Only the existing frozen digest and run may resume')
   attempt=root/'private-attempts'/f"{s['private_attempts']:04}"
   outcome=attempt/'outcome.json'
   if outcome.exists():
    saved=load(outcome);result=saved['result_id'];r=load(root/'results'/(result+'.json'),64*1048576)
    if 'r-'+digest(r)!=result or any(r.get(k)!=s[k] for k in ('run_id','candidate_digest','card_digest','runtime_digest')) or r.get('private_attempt')!=s['private_attempts']:raise Error('stale_private_result')
    stage='complete' if r.get('quality_passed') else 'cancelled' if r['status']=='cancelled' else 'failed'
    if saved['stage']!=stage:raise Error('stale_private_result')
   else:
    # Native gate execution has no resumable checkpoint. Preserve its evidence
    # and consume the same attempt; never reopen hidden input to score it again.
    r={'status':'failed','quality_passed':False,'eligible':False,'reason_codes':['private_attempt_interrupted'],'partial_evidence_preserved':True}
    stage='failed';result=save_outcome(root,s,r,stage)
   s=l.transition(stage,result_id=result,completed_at=now())
   return private_reply(s,result)
  elif s['stage']!='frozen':raise Error('private_test_consumed_or_not_frozen')
  elif resume:raise Error('no_interrupted_private_attempt')
  cid=s['candidate_digest'];bridge.verify_candidate(root/'candidates'/cid)
  # The append and fsync happen before even the private manifest is opened.
  s=l.transition('private_started',candidate_digest=cid,private_attempts=s['private_attempts']+1,private_started_at=s.get('private_started_at',now()))
  attempt=root/'private-attempts'/f"{s['private_attempts']:04}"
  try:
   c=load(root/'frozen-card.json');require_primary_resources(policy,c)
   rows=private_rows(root,policy,c);r=bridge.evaluate(root/'candidates'/cid,rows,c,attempt,'full','required',root/'cancel-private.json',data_scope='private',train_hashes=s.get('training_object_sha256',[]))
   if runtime_fingerprint(policy)['runtime_digest']!=s['runtime_digest']:raise Error('runtime_changed_during_private_evaluation')
   stage='complete' if r.get('quality_passed') else 'cancelled' if r['status']=='cancelled' else 'failed'
  except BaseException as ex:
   r={'status':'cancelled' if isinstance(ex,KeyboardInterrupt) else 'failed','quality_passed':False,'eligible':False,'reason_codes':[getattr(ex,'code','private_evaluation_error')],'error':str(ex)[:2000]};stage=r['status']
  result=save_outcome(root,s,r,stage)
  s=l.transition(stage,result_id=result,completed_at=now())
  return private_reply(s,result)

def save_outcome(root,s,r,stage):
 r.update(candidate_digest=s['candidate_digest'],run_id=s['run_id'],card_digest=s['card_digest'],runtime_digest=s['runtime_digest'],private_attempt=s['private_attempts'])
 result='r-'+digest(r);save(root/'results'/(result+'.json'),r,0o400)
 save(root/'private-attempts'/f"{s['private_attempts']:04}"/'outcome.json',{'stage':stage,'result_id':result},0o400)
 return result

def private_reply(s,result):
 return reply(s['run_id'],s['stage'],candidate_digest=s['candidate_digest'],metrics={'test_consumed':True,'disclosed':False,'owner_result_id':result,'private_attempts':s['private_attempts']})

def disclose(root):
 root=Path(root).absolute();boundary(root,False);l=Ledger(root/'state');s=l.read()
 if s['stage'] not in ('complete','failed','cancelled'):raise Error('private_result_not_terminal')
 r=load(root/'results'/(s['result_id']+'.json'),64*1048576)
 if 'r-'+digest(r)!=s['result_id']:raise Error('stale_private_result')
 aggregate={'run_id':s['run_id'],'candidate_digest':s['candidate_digest'],'status':r['status'],'eligible':r.get('eligible',False),'reason_codes':r.get('reason_codes',[]),'accounting':r.get('accounting',{}).get('development'),'timing':r.get('timing'),'test_consumed':True,'adaptive_reuse_forbidden':True}
 if r.get('workload')=='mutable_store':
  mm=r.get('mutable_metrics',{});aggregate['workload']='mutable_store';aggregate['mutable']={'development':mm.get('development'),'operation_latency':mm.get('operation_latency_aggregate',{}),'process_crash_validated':r.get('process_crash_validated',False),'power_loss_certified':False,'encoding_floor_applied':False}
 def mark(s):s['disclosed']=True;s['disclosed_at']=s.get('disclosed_at',now());return s
 l.update('disclose',mark);save(root/'disclosure.json',aggregate,0o400);return reply(s['run_id'],'disclosed',candidate_digest=s['candidate_digest'],metrics=aggregate)
