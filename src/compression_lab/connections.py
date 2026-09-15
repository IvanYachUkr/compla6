"""Owned-only host configuration edits. No authentication or model operations."""
from __future__ import annotations
import json,os,re,shutil,subprocess,sys,tomllib
from pathlib import Path
from .util import Error,load,save,atomic,sha,digest,reply,lock
NAME='compression-lab'
START='# compression-lab:begin:v1\n';END='# compression-lab:end:v1\n'
def settings(project):
 # Preserve a venv interpreter symlink: realpath(sys.executable) loses the venv.
 return {'command':str(Path(sys.executable).absolute()),'args':['-m','compression_lab.mcp_server','--workspace',str(project.absolute())]}
def paths(host,project):
 if host=='codex':return project/'.codex/config.toml',['mcp_servers',NAME]
 if host=='zcode':
  native=project/'.zcode/config.json'
  return (native,['mcp','servers',NAME]) if native.exists() else (project/'.agents/mcp.json',['mcpServers',NAME])
 raise Error('unknown_config_host')
def nested(data,keys):
 for k in keys:
  if not isinstance(data,dict):raise Error('invalid_host_configuration')
  if k not in data:return None
  data=data[k]
 return data
def backup(p):
 if p.exists():
  q=p.with_name(p.name+'.compression-lab-backup-'+sha(p)[:16])
  if not q.exists():atomic(q,p.read_bytes(),0o600)
def skill(project,install):
 receipts=project/'.compression-lab-connections';p=project/'.agents/skills/compression-lab/SKILL.md';r=receipts/'skill.json';content=(Path(__file__).parent/'data/SKILL.md').read_bytes()
 if install:
  if p.exists() and p.read_bytes()!=content:raise Error('skill_conflict','Existing shared skill was not changed')
  created=not p.exists()
  if created:atomic(p,content,0o644)
  if not r.exists():save(r,{'created':created,'sha256':sha(p)})
 else:
  if any((receipts/(h+'.json')).exists() for h in ('codex','prime','zcode')):return
  if r.exists():
   record=load(r)
   if record['created'] and p.exists() and sha(p)==record['sha256']:p.unlink()
   r.unlink()
def prime_call(project,args):
 exe=shutil.which('prime-agent')
 if not exe:raise Error('client_unavailable','Prime is not installed here. CLI fallback: compression-lab profile --workspace '+str(project))
 p=subprocess.run([exe,*args],cwd=project,capture_output=True,text=True,timeout=20)
 return p
def connect(host,project,apply=False):
 project=Path(project).absolute()
 if not project.is_dir():raise Error('project_missing')
 if not apply:return _connect(host,project,False)
 with lock(project/'.compression-lab-connect.lock'):return _connect(host,project,True)
def _connect(host,project,apply=False):
 project=Path(project).absolute()
 if not project.is_dir():raise Error('project_missing')
 config=settings(project);receipt=project/'.compression-lab-connections'/f'{host}.json'
 sk=project/'.agents/skills/compression-lab/SKILL.md'
 if sk.exists() and sk.read_bytes()!=(Path(__file__).parent/'data/SKILL.md').read_bytes():raise Error('skill_conflict','Existing skill preserved before any host registration')
 if host=='prime':
  command=['prime-agent','mcp','add',NAME,'--cwd',str(project),'--',config['command'],*config['args']]
  if not apply:return reply(metrics={'host':host,'preview':command,'live_client_verified':False})
  version=prime_call(project,['--version']);v=re.search(r'(\d+)\.(\d+)\.(\d+)',version.stdout)
  if version.returncode or not v or tuple(map(int,v.groups()))<(0,9,2):raise Error('unsupported_prime_version','Requires documented native MCP interface in 0.9.2 or newer')
  current=prime_call(project,['mcp','get',NAME])
  if receipt.exists():
   r=load(receipt)
   if current.returncode==0 and digest(current.stdout)==r['get_digest']:return reply(status='already_connected',metrics={'host':host})
   raise Error('owned_entry_modified','Prime entry preserved')
  if current.returncode==0:raise Error('server_conflict','Existing Prime entry is not owned by Compression Lab')
  p=prime_call(project,command[1:])
  if p.returncode:raise Error('prime_registration_failed',p.stderr[:1000])
  current=prime_call(project,['mcp','get',NAME])
  if current.returncode:raise Error('prime_verification_failed')
  save(receipt,{'host':host,'get_digest':digest(current.stdout),'configuration':config});skill(project,True)
  return reply(status='connected',metrics={'host':host,'client_version':version.stdout.strip(),'protocol_test_required':True})
 p,keys=paths(host,project)
 if p.is_symlink():raise Error('symlink_config')
 if receipt.exists():
  r=load(receipt);p=Path(r['path']);keys=r['keys'];current=p.read_text() if p.exists() else ''
  parsed=tomllib.loads(current) if host=='codex' else json.loads(current or '{}')
  if nested(parsed,keys)!=r['configuration']:raise Error('owned_entry_modified','Existing entry preserved')
  return reply(status='already_connected',metrics={'host':host,'path':str(p)})
 before=p.read_text() if p.exists() else '';parsed=tomllib.loads(before) if host=='codex' else json.loads(before or '{}')
 if nested(parsed,keys) is not None:raise Error('server_conflict','An unowned server entry already exists')
 sk=project/'.agents/skills/compression-lab/SKILL.md'
 if sk.exists() and sk.read_bytes()!=(Path(__file__).parent/'data/SKILL.md').read_bytes():raise Error('skill_conflict')
 created=[]
 if host=='codex':
  block=('\n' if before and not before.endswith('\n') else '')+START+'[mcp_servers.compression-lab]\ncommand = '+json.dumps(config['command'])+'\nargs = '+json.dumps(config['args'])+'\n'+END
  after=before+block;tomllib.loads(after)
 else:
  node=parsed
  for i,k in enumerate(keys[:-1]):
   if k not in node:node[k]={};created.append(keys[:i+1])
   if not isinstance(node[k],dict):raise Error('invalid_host_configuration')
   node=node[k]
  node[keys[-1]]=config;after=json.dumps(parsed,indent=2,ensure_ascii=False)+'\n';block=None
 if not apply:return reply(metrics={'host':host,'path':str(p),'owned_entry':config,'preview_only':True,'live_client_verified':False})
 file_created=not p.exists()
 backup(p);atomic(p,after.encode(),0o600);save(receipt,{'host':host,'path':str(p),'keys':keys,'configuration':config,'block':block,'file_created':file_created,'was_present':bool(before),'created_containers':created})
 skill(project,True)
 return reply(status='connected',metrics={'host':host,'path':str(p),'live_client_verified':False})
def disconnect(host,project,apply=False):
 project=Path(project).absolute()
 if not apply or not project.is_dir():return _disconnect(host,project,apply)
 with lock(project/'.compression-lab-connect.lock'):return _disconnect(host,project,True)
def _disconnect(host,project,apply=False):
 project=Path(project).absolute();receipt=project/'.compression-lab-connections'/f'{host}.json'
 if not receipt.exists():return reply(status='already_disconnected',metrics={'host':host})
 r=load(receipt)
 if host=='prime':
  p=prime_call(project,['mcp','get',NAME])
  if p.returncode==0 and digest(p.stdout)!=r['get_digest']:raise Error('owned_entry_modified','Prime entry preserved')
  if not apply:return reply(metrics={'preview':['prime-agent','mcp','remove',NAME]})
  if p.returncode==0:
   p=prime_call(project,['mcp','remove',NAME])
   if p.returncode:raise Error('prime_remove_failed',p.stderr[:1000])
 else:
  p=Path(r['path'])
  if p.is_symlink():raise Error('symlink_config')
  before=p.read_text() if p.exists() else '';parsed=tomllib.loads(before) if host=='codex' else json.loads(before or '{}')
  current=nested(parsed,r['keys'])
  if current is not None and current!=r['configuration']:raise Error('owned_entry_modified','User-modified entry preserved')
  if host=='codex':
   if current is not None and r['block'] not in before:raise Error('owned_block_modified')
   after=before.replace(r['block'],'',1) if current is not None else before
  else:
   if current is not None:
    node=nested(parsed,r['keys'][:-1]);del node[r['keys'][-1]]
   for keys in reversed(r['created_containers']):
    if nested(parsed,keys)=={}:del nested(parsed,keys[:-1])[keys[-1]]
   after=json.dumps(parsed,indent=2,ensure_ascii=False)+'\n'
  if not apply:return reply(metrics={'host':host,'path':str(p),'remove_only_unchanged_owned_entry':True})
  backup(p);atomic(p,after.encode(),0o600)
 receipt.unlink();skill(project,False);return reply(status='disconnected',metrics={'host':host})
