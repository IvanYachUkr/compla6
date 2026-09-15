#!/usr/bin/env python3
"""Prepare one fresh public diagnostic-feedback arm in the protected runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from compression_lab import baselines
from compression_lab.engine import Engine
from compression_lab.util import save

p = argparse.ArgumentParser()
p.add_argument('--workspace', type=Path, required=True)
p.add_argument('--card', type=Path, required=True)
p.add_argument('--materials', type=Path, required=True)
p.add_argument('--diagnostics', action='store_true')
a = p.parse_args()
engine = Engine.init(a.workspace, a.card)
agent = a.workspace / 'workbench/agent'
(agent / 'scratch').mkdir(parents=True)
(agent / 'docs').mkdir()
baselines.create(agent / 'candidate', 'zstd', 1)
_, rows = engine.data()
baselines.create(a.workspace / 'workbench/baseline-source', 'zstd', 1, rows, True)
for name in ('CANDIDATE_ABI.md', 'AGENT_WORKFLOW.md', 'hcb_reference.py', 'stream_reference.py'):
    shutil.copyfile(a.materials / name, agent / 'docs' / name)
shutil.copyfile(a.materials / 'lab_client.py', a.workspace / 'workbench/lab_client.py')
helper = """from lab_client import LabClient

class ResearchClient(LabClient):
    async def feedback(self, reference_result_id, candidate_result_id):
        return await self.call('compare', result_ids=[reference_result_id, candidate_result_id], diagnostics=DIAGNOSTICS)
""".replace('DIAGNOSTICS', repr(a.diagnostics))
(a.workspace / 'workbench/trial_feedback.py').write_text(helper)
report = {'model_calls': 0, 'status': 'baseline_setup', 'diagnostics': a.diagnostics, 'baseline_results': []}
save(a.workspace / 'pilot-setup.json', report)
for path in (agent / 'candidate', a.workspace / 'workbench/baseline-source'):
    cid = engine.register(path, 'baseline')['candidate_digest']
    res = engine.evaluate(cid, 'full', 'baseline', wait=True)
    report['baseline_results'].append(res)
    save(a.workspace / 'pilot-setup.json', report)
    if not res['metrics']['quality_passed']:
        raise RuntimeError(json.dumps(res))
report.update(status='ready_for_model', seed_inventory={
    str(path.relative_to(a.workspace / 'workbench')): hashlib.sha256(path.read_bytes()).hexdigest()
    for directory in (agent / 'candidate', a.workspace / 'workbench/baseline-source', agent / 'docs')
    for path in directory.rglob('*') if path.is_file()}, runtime_digest=engine.state()['runtime_digest'],
    card_digest=engine.state()['card_digest'], run_id=engine.state()['run_id'])
save(a.workspace / 'pilot-setup.json', report)
print(json.dumps({'workspace': str(a.workspace), 'status': report['status'],
                  'runtime_digest': report['runtime_digest'], 'card_digest': report['card_digest']}), flush=True)
