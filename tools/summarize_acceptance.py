#!/usr/bin/env python3
"""Read immutable results; do not recompute codec metrics or select fastest trials."""
import csv,json
from pathlib import Path
from compression_lab.engine import Engine
from compression_lab.util import save
from compression_lab.accounting import compare
base=Path(__file__).resolve().parent.parent/'results/acceptance'
rows=[];controls=[]
for family in ('hadoop','openstack','bytes'):
 e=Engine(base/family);table=e.baseline_table();save(base/(family+'-baselines.json'),table)
 for entry in table['metrics']['rows']:
  r=e.raw(entry['result_id']);a=r['accounting'];f=r['fixed_costs'];t=r['timing']
  assert r['quality_passed'] and all(len(r['timing_trials'][p])==7 for p in ('encode','decode'))
  row={'dataset':family,'baseline':entry['name'],'candidate_digest':r['candidate_digest'],'result_id':entry['result_id'],
       'quality_passed':r['quality_passed'],'eligible':r['eligible'],'reason_codes':r['reason_codes'],
       'canonical_bytes':a['all']['canonical_bytes'],'framed_archive_bytes':a['all']['actual']['archive_bytes'],
       'archive_compression_ratio':a['all']['compression_ratio'],'historical_primary_bytes':a['all']['actual']['historical_primary_bytes'],
       'deployment_total_bytes':a['all']['actual']['deployment_total_bytes'],
       'packed_source_bytes':f['packed_source_bytes'],'fixed_bytes':f['fixed_bytes'],'config_bytes':f['config_bytes'],
       'binary_bytes':f['binary_bytes'],'nonplatform_dependency_bytes':f['nonplatform_dependency_bytes'],
       'encode_MBps':t['encode']['median_decimal_MB_per_second'],'decode_MBps':t['decode']['median_decimal_MB_per_second'],
       'encode_relative_MAD':t['encode']['relative_MAD'],'decode_relative_MAD':t['decode']['relative_MAD'],
       'peak_rss_bytes':max(t[p]['peak_rss_bytes'] for p in ('encode','decode')),'encoding_trials':7,'decoding_trials':7,
       'development_horizon_deployment_bytes':(a['development']['projection'] or {}).get('deployment_total_bytes')}
  rows.append(row)
 ref=next(x for x in table['metrics']['rows'] if x['name']=='zstd-1')
 export=base/(family+'-baseline.zip')
 if not export.exists():save(base/(family+'-export.json'),e.export(ref['result_id'],export))
 save(base/(family+'-paired-comparisons.json'),{x['name']:compare(e.raw(x['result_id'])['objects'],e.raw(ref['result_id'])['objects']) for x in table['metrics']['rows'] if x['result_id']!=ref['result_id']})
 for rid in e.state()['results']:
  r=e.raw(rid)
  if r['track'] in ('deterministic','random'):
   assert r['quality_passed'] and all(len(r['timing_trials'][p])==7 for p in ('encode','decode'))
   controls.append({'dataset':family,'method':r['track'],'result_id':rid,'candidate_digest':r['candidate_digest'],'quality_passed':True,'eligible':r['eligible'],'wall_seconds':r['wall_seconds'],'reason_codes':r['reason_codes'],'timing':r['timing']})
save(base/'summary.json',rows);save(base/'controls-summary.json',controls)
with (base/'summary.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
save(base/'validation.json',{'baseline_evaluations':len(rows),'baseline_quality_passed':sum(x['quality_passed'] for x in rows),'control_evaluations':len(controls),'control_quality_passed':sum(x['quality_passed'] for x in controls),'timing_trials_in_this_workspace':14*(len(rows)+len(controls)),'certified':0,'model_calls':0,'hidden_objects':0,'execution':'Installed wheel; long CLI demo interrupted by execution-tool limit; remaining stages completed synchronously by dataset/control. Separate tools/acceptance.sh succeeded as a single command.'})
print(json.dumps({'baselines':len(rows),'controls':len(controls),'trials':14*(len(rows)+len(controls)),'zstd':[{k:x[k] for k in ('dataset','canonical_bytes','framed_archive_bytes','archive_compression_ratio','encode_MBps','decode_MBps')} for x in rows if x['baseline']=='zstd-1']},indent=2))
