#!/usr/bin/env python3
"""Installed-package, one-control acceptance. All native release gates still run.

Unlike `compression-lab demo`, which evaluates three families and two controls,
this bounded installation check evaluates one real Zstandard control on Hadoop.
No timing trial or quality gate is removed. Smoke results cannot certify speed.
"""
import argparse,json,sys
from pathlib import Path
from compression_lab.demo import inputs
from compression_lab.engine import Engine,doctor
from compression_lab.util import Error,save

p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
root=Path(args.output).absolute()
try:
 if root.exists() and any(root.iterdir()):raise Error('demo_output_not_empty')
 root.mkdir(parents=True,exist_ok=True)
 save(root/'doctor.json',doctor());save(root/'release-preflight.json',doctor(True))
 e=Engine.init(root/'workspace',inputs(root,'hadoop'))
 save(root/'profile.json',e.profile())
 table=e.prepare_baselines([('zstd',1,False)]);save(root/'baselines.json',table)
 rows=table['metrics']['rows']
 if table['metrics']['unavailable'] or len(rows)!=1 or not rows[0]['quality_passed']:
  raise Error('acceptance_failed',json.dumps(table['metrics']))
 result_id=rows[0]['result_id'];raw=e.raw(result_id)
 trials=raw['timing_trials']
 assert all(len(trials[k])==7 for k in ('encode','decode'))
 save(root/'status.json',e.status(result=result_id))
 save(root/'artifact-preview.json',e.artifact(result_id,'raw.json',0,4096))
 save(root/'export.json',e.export(result_id,root/'baseline.zip'))
 result={'status':'complete','quality_passed':True,'eligible':raw['eligible'],
         'reason_codes':raw['reason_codes'],'run_id':raw['run_id'],
         'candidate_digest':raw['candidate_digest'],'result_id':result_id,
         'encoding_trials':len(trials['encode']),'decoding_trials':len(trials['decode']),
         'model_calls':0,'private_objects':0,'scope':'One real Zstandard control; all Hadoop public objects; full quality gates; smoke only.'}
 save(root/'summary.json',result);print(json.dumps(result))
except Exception as ex:
 print(json.dumps({'status':'failed','reason_code':getattr(ex,'code',type(ex).__name__),'error':str(ex)}))
 sys.exit(1)
