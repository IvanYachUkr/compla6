#!/usr/bin/env python3
"""Standard-library test runner preserving full output and machine-readable totals."""
import argparse,json,sys,time,unittest
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output',default='results/final-tests');p.add_argument('--modules',nargs='+');args=p.parse_args()
root=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(root/'src'));sys.path.insert(0,str(root/'tests'))
out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);start=time.monotonic()
with out.with_suffix('.txt').open('w') as f:
 suite=unittest.defaultTestLoader.loadTestsFromNames(args.modules) if args.modules else unittest.defaultTestLoader.discover(str(root/'tests'))
 result=unittest.TextTestRunner(stream=f,verbosity=2).run(suite)
value={'tests_run':result.testsRun,'passed':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),'failures':len(result.failures),'errors':len(result.errors),'skipped':[{'test':str(t),'reason':why} for t,why in result.skipped],'success':result.wasSuccessful(),'elapsed_seconds':time.monotonic()-start}
out.with_suffix('.json').write_text(json.dumps(value,indent=2)+'\n');print(json.dumps(value));sys.exit(0 if result.wasSuccessful() else 1)
