"""Public-only dataset cards, digest verification, grouped splits, profiles."""
from __future__ import annotations
import collections, copy, math, shutil, hashlib, re
from pathlib import Path
from .util import Error,load,save,safe,sha,digest
from .hcb import decode_hcb
from .stream import ALIAS_RE,MAX_ALIAS_BYTES

KEYS={'schema_version','dataset_id','adapter','public_manifest','group_key','development_policy','input_domain','objective','limits','search_budget','scope','timing_policy','workload','mutable_policy','runtime_scenarios','screening_policy','resource_profiles','accounting_policy','evaluation_mode','baseline_visibility'}
KEYS.update({'implementation_policy','primary_size_policy','hypothesis_policy'})

def public_partitions(card):
 return ('corpus',) if card.get('evaluation_mode','split')=='whole_dataset' else ('train','development')

def fitting_partition(card):
 return public_partitions(card)[0]

def scored_partition(card_or_result):
 return 'corpus' if card_or_result.get('evaluation_mode','split')=='whole_dataset' else 'development'

def encoding_timing_key(card_or_result):
 """Resolve the qualified encoding measurement without reinterpreting old cards."""
 policy=card_or_result.get('timing_policy',card_or_result)
 return 'combined' if policy.get('encoding_floor_scope')=='combined' else 'encode'

def validate_profiles(value):
 if not isinstance(value,dict) or set(value)!={'schema_version','primary','profiles'} or value['schema_version']!=1 or not isinstance(value['profiles'],list) or not 1<=len(value['profiles'])<=8:raise Error('invalid_resource_profiles')
 ids=set()
 for p in value['profiles']:
  if not isinstance(p,dict) or set(p)!={'id','threads','cpus'} or not isinstance(p['id'],str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',p['id']) or p['id'] in ids:raise Error('invalid_resource_profile')
  if type(p['threads']) is not int or not 1<=p['threads']<=64 or not isinstance(p['cpus'],list) or not p['cpus'] or len(set(p['cpus']))!=len(p['cpus']) or any(type(x) is not int or x<0 for x in p['cpus']):raise Error('invalid_resource_profile')
  ids.add(p['id'])
 if value['primary'] not in ids:raise Error('missing_primary_resource_profile')
 return value

def primary_profile(card):
 return card.get('resource_profiles',{}).get('primary','legacy-single')

def select_profile(card,profile_id=None):
 """Resolve an internal evaluation view without changing the immutable card."""
 from .resources import resolve_resources
 c=copy.deepcopy(card);name=primary_profile(c) if profile_id is None else profile_id
 profiles=c.get('resource_profiles')
 if profiles:
  validate_profiles(profiles);selected=next((p for p in profiles['profiles'] if p['id']==name),None)
  if selected is None:raise Error('unknown_resource_profile',str(name))
  resources=resolve_resources(selected['threads'],selected['cpus'])
 else:
  if name!='legacy-single':raise Error('unknown_resource_profile',str(name))
  resources=resolve_resources(1)
 c['limits'].update(threads=resources['threads'],cpus=resources['cpus'])
 c['_resource_profile']={'id':name,**resources,'memory_bytes':c['limits']['memory_bytes']}
 c['_primary_profile']=primary_profile(c)
 return c
def validate(c):
 if not isinstance(c,dict) or set(c)-KEYS:raise Error('invalid_public_card','Unknown public card fields are forbidden, including private metadata')
 if c.get('baseline_visibility','visible') not in ('visible','hidden'):raise Error('invalid_baseline_visibility')
 if c.get('implementation_policy','open') not in ('open','from_scratch'):raise Error('invalid_implementation_policy')
 if c.get('hypothesis_policy','optional') not in ('optional','required'):raise Error('invalid_hypothesis_policy')
 if c.get('primary_size_policy','strict-deployment-v1') not in ('strict-deployment-v1','standard-codec-available-v1'):raise Error('invalid_primary_size_policy')
 if c.get('schema_version')!=1 or c.get('adapter') not in ('bytes','hcb1','relational_bundle'):raise Error('unsupported_adapter')
 if not isinstance(c.get('dataset_id'),str) or not c.get('public_manifest'):raise Error('invalid_public_card')
 from .workloads.cards import validate_selection,workload
 validate_selection(c)
 mode=c.get('evaluation_mode','split')
 if mode not in ('split','whole_dataset'):raise Error('invalid_evaluation_mode')
 if mode=='whole_dataset' and (workload(c)=='mutable_store' or 'development_policy' in c):raise Error('incompatible_whole_dataset_policy')
 if 'resource_profiles' in c:validate_profiles(c['resource_profiles'])
 if c.get('accounting_policy','standalone-v1') not in ('standalone-v1','supervisor-decoder-v1'):raise Error('invalid_accounting_policy')
 if 'runtime_scenarios' in c:
  from .deployment import validate_scenarios
  validate_scenarios(c['runtime_scenarios'])
 if c.get('primary_size_policy')=='standard-codec-available-v1':
  from .deployment import STANDARD_CODECS
  profiles=c.get('runtime_scenarios',{}).get('profiles',[])
  profile=next((p for p in profiles if p['id']=='standard-codec-available-v1'),None)
  if c.get('accounting_policy')!='supervisor-decoder-v1' or profile is None or any(x['soname'] not in STANDARD_CODECS for x in profile['libraries']):raise Error('invalid_standard_codec_policy')
 if 'screening_policy' in c:
  from .screening import validate_policy
  validate_policy(c['screening_policy'])
 if workload(c)=='mutable_store' and any(k in c for k in ('runtime_scenarios','screening_policy','resource_profiles','accounting_policy')):raise Error('static_policy_on_mutable_workload')
 o=c.get('objective',{});lim=c.get('limits',{});b=c.get('search_budget',{})
 if workload(c)=='mutable_store':
  if o.get('encode_floor_bytes_per_second') is not None or o.get('decode_floor_bytes_per_second') is not None:raise Error('invalid_speed_policy','Mutable operations have no static throughput floor')
 elif o.get('encode_floor_bytes_per_second')!=100_000_000 or o.get('decode_floor_bytes_per_second') is not None:raise Error('invalid_speed_policy','v1 static: ENCODING ONLY, 100 decimal MB/s')
 if o.get('rank_by')!='deployment_total_bytes' or not isinstance(o.get('deployment_canonical_bytes'),int) or o['deployment_canonical_bytes']<=0:raise Error('invalid_objective')
 if type(lim.get('threads')) is not int or not 1<=lim['threads']<=64 or (not c.get('resource_profiles') and lim['threads']!=1) or not 64*1048576<=lim.get('memory_bytes',0)<=16*1024**3 or not 0<lim.get('timeout_seconds',0)<=3600:raise Error('invalid_limits')
 if c.get('resource_profiles'):
  primary=next(p for p in c['resource_profiles']['profiles'] if p['id']==primary_profile(c))
  if lim['threads']!=primary['threads'] or ('cpus' in lim and lim['cpus']!=primary['cpus']):raise Error('inconsistent_primary_resource_limits')
 if not isinstance(b.get('candidate_evaluations'),int) or b['candidate_evaluations']<=0 or b.get('wall_seconds',0)<=0:raise Error('invalid_budget')
 t=c.get('timing_policy',{})
 if 'operation' in t and t['operation'] not in ('end-to-end-encode-v1','offline-plus-online-v1'):raise Error('invalid_timing_operation')
 if t.get('operation')=='offline-plus-online-v1':
  if workload(c)=='mutable_store' or t.get('encoding_floor_scope') not in ('online','combined'):raise Error('invalid_encoding_floor_scope')
 elif 'encoding_floor_scope' in t:raise Error('invalid_encoding_floor_scope')
 if 'offline_timeout_seconds' in lim and (isinstance(lim['offline_timeout_seconds'],bool) or not isinstance(lim['offline_timeout_seconds'],(int,float)) or not math.isfinite(lim['offline_timeout_seconds']) or not 0<lim['offline_timeout_seconds']<=3600):raise Error('invalid_offline_timeout')
 if t.get('role','smoke') not in ('smoke','benchmark') or t.get('warmup_trials',0)!=0 or not .01<=t.get('noise_relative_mad_limit',.1)<=.25:raise Error('invalid_timing_policy')
 if mode=='whole_dataset':
  if t.get('scope')!='whole-dataset-v1' or c.get('accounting_policy')!='supervisor-decoder-v1':raise Error('whole_dataset_requires_actual_corpus_timing_and_accounting')
 else:
  if t.get('scope','train-plus-development-v1') not in ('train-plus-development-v1','validation-only-v2'):raise Error('invalid_timing_scope')
  if c.get('accounting_policy')=='supervisor-decoder-v1' and t.get('scope')!='validation-only-v2':raise Error('decoder_policy_requires_validation_timing')
 return c

def read_source(card_path):
 card_path=Path(card_path);c=validate(load(card_path));mp=safe(card_path.parent,c['public_manifest']);m=load(mp)
 if m.get('schema_version')!=1 or set(m)-{'schema_version','adapter','dataset_id','scope','objects','ordering','provenance'}:raise Error('invalid_manifest')
 rows=m.get('objects',[])
 if not rows or len(rows)>1000000:raise Error('invalid_object_count')
 groups={};aliases=set();out=[]
 allowed={'alias','canonical_bytes','canonical_sha256','file_count','group_alias','source_group','group','path','raw_bytes','split','order','provenance'}
 for i,r in enumerate(rows):
  if set(r)-allowed:raise Error('invalid_object_fields')
  a=r.get('alias','');s=r.get('split');g=r.get(c.get('group_key','source_group')) or r.get('source_group') or r.get('group_alias') or r.get('group')
  if not ALIAS_RE.fullmatch(a) or len(a.encode())>MAX_ALIAS_BYTES or a in aliases:raise Error('invalid_alias')
  if s not in public_partitions(c):raise Error('private_split_in_public_card','Partition is not permitted by the declared evaluation mode')
  if not isinstance(g,str) or not g:raise Error('missing_source_group')
  if g in groups and groups[g]!=s:raise Error('group_leakage',g)
  aliases.add(a);groups[g]=s;p=safe(mp.parent,r['path'])
  if p.stat().st_size!=r.get('canonical_bytes') or sha(p)!=r.get('canonical_sha256'):raise Error('dataset_digest_mismatch',a)
  if c['adapter']=='hcb1':
   v=decode_hcb(p.read_bytes())
   if 'file_count' in r and len(v)!=r['file_count']:raise Error('wrong_file_count')
   if 'raw_bytes' in r and sum(len(b) for _,b in v)!=r['raw_bytes']:raise Error('wrong_raw_bytes')
  if c['adapter']=='relational_bundle':
   from .workloads.cards import validate_bundle
   validate_bundle(c,p.read_bytes())
  out.append({'alias':a,'group':g,'split':s,'canonical_bytes':p.stat().st_size,'canonical_sha256':sha(p),'order':i,'source':p})
 if set(groups.values())!=set(public_partitions(c)):raise Error('missing_public_split')
 return c,out

def snapshot(card_path,root):
 root=Path(root);c,rows=read_source(card_path);(root/'public/objects').mkdir(parents=True)
 out=[]
 for r in rows:
  q={k:v for k,v in r.items() if k!='source'};q['path']='objects/'+q['canonical_sha256']+'.bin';p=root/'public'/q['path']
  if not p.exists():shutil.copyfile(r['source'],p);p.chmod(0o444)
  out.append(q)
 c={**c,'public_manifest':'public/manifest.json'};m={'schema_version':1,'objects':out,'adapter':c['adapter'],'ordering':'manifest order; packed transport aliases sorted ordinally'}
 save(root/'dataset-card.json',c,0o444);save(root/'public/manifest.json',m,0o444)
 return c,digest({'card':c,'manifest':m})

def read_metadata(root,expected):
 """Read the sealed card/manifest and safe file metadata, NOT payload integrity.

 Registration/discovery may use this view. It cannot certify a result. Full
 evaluation and export call read_snapshot; screens hash the exact selected bytes.
 """
 root=Path(root);c=validate(load(safe(root,'dataset-card.json')));m=load(safe(root,c['public_manifest']))
 if not expected or digest({'card':c,'manifest':m})!=expected:raise Error('stale_card_digest')
 return c,[{**r,'source':safe(root/'public',r['path'])} for r in m['objects']]

def read_snapshot(root,expected=None):
 root=Path(root)
 if expected:c,rows=read_metadata(root,expected)
 else:
  c=validate(load(safe(root,'dataset-card.json')));m=load(safe(root,c['public_manifest']))
  rows=[{**r,'source':safe(root/'public',r['path'])} for r in m['objects']]
 for r in rows:
  p=r['source']
  if p.stat().st_size!=r['canonical_bytes'] or sha(p)!=r['canonical_sha256']:raise Error('stale_data_digest',r['alias'])
 return c,rows

def profile(root,expected=None):
 c,rows=read_snapshot(root,expected);counts=collections.Counter();splits={};groups=set();total=nl=digits=0
 for r in rows:
  b=r['source'].read_bytes();counts.update(b);total+=len(b);nl+=b.count(b'\n');digits+=sum(b.count(bytes([i])) for i in range(48,58));groups.add(r['group'])
  s=splits.setdefault(r['split'],{'objects':0,'canonical_bytes':0,'groups':set()});s['objects']+=1;s['canonical_bytes']+=len(b);s['groups'].add(r['group'])
 ent=-sum(n/total*math.log2(n/total) for n in counts.values()) if total else 0
 extra={'workload':c.get('workload','independent_objects'),'evaluation_mode':c.get('evaluation_mode','split'),'scored_partition':scored_partition(c)}
 if c['adapter']=='relational_bundle':
  from .workloads.relational import profile as relational_profile
  extra['relational']={'format':'RLB1','objects':[{'alias':r['alias'],**relational_profile(r['source'].read_bytes())} for r in rows]}
 return {**extra,'dataset_id':c['dataset_id'],'adapter':c['adapter'],'objects':len(rows),'canonical_bytes':total,'source_groups':len(groups),
  'splits':{k:{**v,'groups':len(v['groups'])} for k,v in splits.items()},'byte_entropy_bits':ent,'newline_count':nl,'ascii_digit_bytes':digits,
  'examples':[{'alias':r['alias'],'prefix_hex':r['source'].read_bytes()[:32].hex()} for r in rows[:2]],
  'scope':c.get('scope','operator-supplied public data'),'dictionary_policy':('Entire corpus may be used for artifact fitting and specialization.' if c.get('evaluation_mode')=='whole_dataset' else 'Train groups only; development never enters artifact training.')}

def example_card(dataset_id,adapter='bytes'):
 """Historical split smoke fixture. Use research_card for new research."""
 return {'schema_version':1,'dataset_id':dataset_id,'adapter':adapter,'public_manifest':'manifest.json','group_key':'source_group',
 'input_domain':'Opaque independent bytes' if adapter=='bytes' else 'Valid HCB1, arbitrary content bytes',
 'development_policy':{'folds':5,'seed':20260906},'scope':'Smoke inputs only, not a held-out benchmark',
 'objective':{'encode_floor_bytes_per_second':100000000,'decode_floor_bytes_per_second':None,'deployment_canonical_bytes':100000000,'rank_by':'deployment_total_bytes'},
 'limits':{'threads':1,'memory_bytes':2147483648,'timeout_seconds':120,'output_bytes':8*1024**3},'search_budget':{'candidate_evaluations':30,'wall_seconds':7200},
 'timing_policy':{'role':'smoke','warmup_trials':0,'noise_relative_mad_limit':.1}}

def research_card(dataset_id,adapter='bytes'):
 """New research defaults; the owner still supplies exact resources and data."""
 from .deployment import standard_codec_scenario
 card=example_card(dataset_id,adapter)
 card.pop('development_policy')
 card.update(evaluation_mode='whole_dataset',scope='Complete supplied dataset',
             accounting_policy='supervisor-decoder-v1',implementation_policy='from_scratch',
             primary_size_policy='standard-codec-available-v1',hypothesis_policy='required',
             runtime_scenarios={'schema_version':1,'profiles':[standard_codec_scenario()]})
 card['timing_policy'].update(scope='whole-dataset-v1',operation='offline-plus-online-v1',encoding_floor_scope='combined')
 card['limits']['offline_timeout_seconds']=3600
 return card

def create_research_input(output,inputs,dataset_id,implementation='from_scratch',baseline_visibility='visible',smoke=False):
 """Prepare a new whole-corpus card; never rewrite existing data or start a job."""
 from .util import ident
 ident(dataset_id);output=Path(output).absolute();inputs=[Path(p) for p in inputs]
 if not inputs or any(not p.is_file() for p in inputs):raise Error('input_file_required')
 card=research_card(dataset_id)
 card.update(implementation_policy=implementation,baseline_visibility=baseline_visibility)
 card['timing_policy']['role']='smoke' if smoke else 'benchmark'
 validate(card)
 if output.exists():raise Error('output_exists')
 (output/'objects').mkdir(parents=True);rows=[]
 for i,source in enumerate(inputs):
  alias=f'object-{i:06d}';relative=f'objects/{alias}.bin';dest=output/relative
  shutil.copyfile(source,dest)
  rows.append({'alias':alias,'source_group':alias,'split':'corpus','path':relative,
               'canonical_bytes':dest.stat().st_size,'canonical_sha256':sha(dest)})
 card['objective']['deployment_canonical_bytes']=max(1,sum(r['canonical_bytes'] for r in rows))
 save(output/'manifest.json',{'schema_version':1,'objects':rows})
 save(output/'card.json',card)
 return output/'card.json'
