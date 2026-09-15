#!/usr/bin/env python3
"""Collect committed public Sol results without further evaluation or model calls."""
from pathlib import Path
from compression_lab.engine import Engine
from compression_lab.util import load,save

root=Path('/var/lib/compression-lab/hadoop-sol-high-0.2.1-2026-09-06')
engine=Engine(root);state=engine.state()
assert state['active_job'] is None
rows=[]
for rid in state['results']:
    result=engine.result(rid);raw=engine.raw(rid)
    name='zstd-1-dict' if raw['track']=='baseline' else 'sol-prefix-'+raw['depth']
    rows.append({'name':name,'result':result,'raw':raw})
full=[x for x in rows if x['raw']['depth']=='full']
report={'status':'complete','scope':'Fresh public Sol/high experiment on the fixed Hadoop split; no Luna solution or fresh private evaluation',
        'model':'openai-codex/gpt-5.6-sol','thinking':'high','results':rows,
        'comparison':engine.compare([x['result']['metrics']['result_id'] for x in full]),
        'resume':engine.resume(),'state':state,'runtime':load(root/'runtime.json'),
        'registration_failure_attribution':'Two permission failures on agent-owned seed files; setup friction; attempts preserved and charged',
        'model_calls_by_collector':0}
save(root/'sol-final-summary.json',report)
print('Collected '+str(len(rows))+' public results')
