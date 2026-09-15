#!/usr/bin/env python3
"""Initialize a fresh public Sol pilot and recheck its fixed conventional control."""
from pathlib import Path
import shutil
from compression_lab import baselines
from compression_lab.engine import Engine
from compression_lab.util import save,load

root=Path('/var/lib/compression-lab/hadoop-sol-high-0.2.1-2026-09-06')
engine=Engine.init(root,Path('/var/lib/compression-lab/hadoop-input-2026-09-06/card.json'))
agent=root/'workbench/agent';agent.mkdir()
baselines.create(agent/'candidate','zstd',1)
(agent/'scratch').mkdir();(agent/'docs').mkdir()
prior=Path('/var/lib/compression-lab/hadoop-public-2026-09-06/workbench/agent/docs')
for name in ('hcb_reference.py','stream_reference.py'):
 shutil.copyfile(prior/name,agent/'docs'/name)
source=root/'workbench/baseline-source'
shutil.copytree(Path('/var/lib/compression-lab/hadoop-public-0.2.1-2026-09-06/candidates/e07e41d241aeacd7ac339cc9d98fb0dcca0a0b5fd1aa279a32235aea19b79b90/source'),source)
reg=engine.register(source,'baseline')
report={'status':'ready_for_model','model_calls':0,'scope':'Fresh Sol/high public attempt; fixed existing train/development split; no Luna source or result supplied','baseline_candidate':reg['candidate_digest'],'runtime':load(root/'runtime.json'),'run_id':engine.state()['run_id']}
save(root/'pilot-setup.json',report)
print('SETUP_READY',flush=True)
result=engine.evaluate(reg['candidate_digest'],'full','baseline',True)
raw=engine.raw(result['metrics']['result_id'])
report.update(status='baseline_complete',baseline_result=result,baseline_raw=raw)
save(root/'pilot-setup.json',report)
print('BASELINE_COMPLETE '+result['status'],flush=True)
