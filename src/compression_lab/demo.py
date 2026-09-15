"""One-command, model-free public acceptance demonstration."""
import csv,hashlib,random,shutil
from pathlib import Path
from .util import Error,load,save,reply,sha
from . import dataset,baselines
from .engine import Engine,doctor
# Illustrative horizon from the supplied DESIGN.md; never infer an absent test size.
HORIZONS={'hadoop':100000000,'openstack':100000000,'bytes':100000000}
def inputs(root,family):
 path=root/('input-'+family)
 if family!='bytes':
  shutil.copytree(Path(__file__).parent/'data/examples'/family,path)
  original=load(path/'manifest.json');rows=[]
  for r in original['objects']:
   row={k:r[k] for k in ('alias','split','canonical_bytes','canonical_sha256','path','file_count','raw_bytes') if k in r};row['source_group']=r.get('source_group',r.get('group_alias',r.get('group')));rows.append(row)
 else:
  path.mkdir();rng=random.Random(20260906);bodies=[b'',bytes(range(256))*32,b'\x00'*65536,b'line\r\n\n\rno-final-newline',b'000001\t-0001.00\t001.0000\n'*4096,b'\xff\xfe\x80\x00'*16384,rng.randbytes(131072),b'previously unseen prefix ZETA-09 000002\r\n'*2048];rows=[]
  for i,b in enumerate(bodies):
   name=f'object-{i:03}.bin';(path/name).write_bytes(b);rows.append({'alias':f'bytes-{i:03}','source_group':f'group-{i:03}','split':'train' if i<6 else 'development','canonical_bytes':len(b),'canonical_sha256':hashlib.sha256(b).hexdigest(),'path':name})
 save(path/'manifest.json',{'schema_version':1,'scope':'public-smoke-only','objects':rows});card=dataset.example_card(family+'-public-smoke','bytes' if family=='bytes' else 'hcb1');card['objective']['deployment_canonical_bytes']=HORIZONS[family];save(path/'card.json',card);return path/'card.json'
def run(output,matrix=False):
 output=Path(output).absolute()
 if output.exists() and any(output.iterdir()):raise Error('demo_output_not_empty')
 output.mkdir(parents=True,exist_ok=True);save(output/'doctor.json',doctor());save(output/'release-preflight.json',doctor(True));summary=[];fail=[]
 for family in ('hadoop','openstack','bytes'):
  e=Engine.init(output/family,inputs(output,family));save(output/(family+'-profile.json'),e.profile())
  choices=baselines.MATRIX if matrix else [('stored',0,False),('zstd',1,False)]
  table=e.prepare_baselines(choices);save(output/(family+'-baselines.json'),table)
  if table['metrics']['unavailable']:fail.extend(table['metrics']['unavailable'])
  for row in table['metrics']['rows']:
   r=e.raw(row['result_id']);metrics=e.result(row['result_id'])['metrics'];summary.append({'dataset':family,'baseline':row['name'],'candidate_digest':row['candidate_digest'],'result_id':row['result_id'],'quality_passed':row['quality_passed'],'eligible':row['eligible'],'reason_codes':r['reason_codes'],'all_canonical_bytes':metrics['all_canonical_bytes'],'all_archive_bytes':r.get('accounting',{}).get('all',{}).get('actual',{}).get('archive_bytes'),'development_archive_bytes':metrics['development_actual']['archive_bytes'] if metrics.get('development_actual') else None,'encode_MBps':row['encode_MBps'],'decode_MBps':(metrics.get('decode') or {}).get('median_decimal_MB_per_second'),'peak_rss_bytes':max((metrics.get(p) or {}).get('peak_rss_bytes',0) for p in ('encode','decode')),'packed_source_bytes':metrics['packed_source_bytes'],'fixed_bytes':metrics['fixed_bytes'],'binary_bytes':metrics['binary_bytes'],'dependency_bytes':metrics['nonplatform_dependency_bytes'],'projected_deployment_bytes':row['projected_deployment_bytes'],'wall_seconds':metrics['wall_seconds']})
   if not row['quality_passed']:fail.append({'dataset':family,'baseline':row['name'],'reason_codes':r['reason_codes'],'error':r.get('error')})
  # Export a real measured conventional compressor, even when correctly ineligible.
  rows=table['metrics']['rows'];chosen=next((r for r in rows if r['name']=='zstd-1' and r['quality_passed']),None)
  if chosen:save(output/(family+'-export.json'),e.export(chosen['result_id'],output/(family+'-baseline.zip')))
  quality=[r for r in rows if r['quality_passed']]
  reference=next((r for r in quality if r['name']=='zstd-1'),None)
  if reference:
   from .accounting import compare
   comp={r['name']:compare(e.raw(r['result_id'])['objects'],e.raw(reference['result_id'])['objects']) for r in quality if r['name']!='zstd-1'};save(output/(family+'-paired-comparisons.json'),comp)
 save(output/'summary.json',summary);save(output/'failures.json',fail)
 if summary:
  with (output/'summary.csv').open('w',newline='') as f:
   w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
 return reply(status='complete' if not fail else 'failed',metrics={'output':str(output),'evaluations':len(summary),'quality_passed':sum(r['quality_passed'] for r in summary),'eligible':sum(r['eligible'] for r in summary),'smoke_only':True,'failures':fail,'summary_sha256':sha(output/'summary.json'),'model_calls':0,'private_objects':0})
