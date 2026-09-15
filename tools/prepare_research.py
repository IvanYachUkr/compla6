#!/usr/bin/env python3
"""One public evaluator writer: screen a matrix, fully evaluate explicit controls.

No model calls, no private actions, no account/config changes. Not an optimizer.
"""
import argparse
import json
import time
import uuid
from pathlib import Path
from compression_lab.engine import Engine
from compression_lab.util import Error, save, now


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',required=True,type=Path)
    p.add_argument('--screen',nargs='*',default=['stored','zstd--3','zstd-1','zstd-3','zstd-9','zstd-1-dict','zstd-3-dict','lz4-0','brotli-4','xz-3'])
    p.add_argument('--full',nargs='*',default=['stored','zstd-1','lz4-0'])
    a=p.parse_args();e=Engine(a.workspace);card,_=e.metadata()
    path=e.root/('preparation-'+uuid.uuid4().hex+'.json')
    report={'schema_version':1,'started_at':now(),'model_calls':0,'private_operations':0,'requested_screen':a.screen,'requested_full':a.full,'attempts':[]}
    start=time.monotonic()
    if card.get('workload')=='mutable_store':
        report['mutable_reference']=e.prepare_baselines(depth='full')
        report['note']='Mutable oracle/operation contract, no static screen or 100 MB/s gate.'
    else:
        for depth,names in (('screen',a.screen),('full',a.full)):
            for name in names:
                try:row={'recipe_id':name,'depth':depth,'result':e.baseline_trial(name,depth,wait=True)}
                except Error as exc:row={'recipe_id':name,'depth':depth,'error':str(exc),'reason_code':exc.code}
                report['attempts'].append(row);save(path,report)
                print(json.dumps(row),flush=True)
    report.update(completed_at=now(),wall_seconds=time.monotonic()-start,brief=e.brief())
    save(path,report,0o444);print(json.dumps({'receipt':str(path),'status':'completed','interpretation':'Inspect all attempt statuses; completion is not a quality or eligibility claim.'}))

if __name__=='__main__':main()
