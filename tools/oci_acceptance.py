#!/usr/bin/env python3
"""Installed OCI smoke: native build/full gates and persistent recovery, synthetic only."""
import json
from pathlib import Path

from compression_lab import baselines, runner
from compression_lab.engine import Engine
from compression_lab.util import save
from compression_lab.workloads import registry
from compression_lab.workloads.fixtures import create_dataset, movie_bundle
from compression_lab.workloads.mutable_cases import StoreCase

root = Path('/work/acceptance')
root.mkdir(exist_ok=False)
report = {'scope': 'OCI synthetic compatibility and isolation; no throughput/private certification',
          'model_calls': 0, 'runtime': runner.fingerprint(), 'sandbox': runner.probe()}
try:
    assert report['sandbox']['status'] == 'available', report['sandbox']
    engine = Engine.init(root / 'public', create_dataset(root / 'data', 'static_relational'))
    baselines.create(root / 'native', 'zstd', 1)
    registered = engine.register(root / 'native')
    result = engine.evaluate(registered['candidate_digest'], 'full', wait=True)
    assert result['metrics']['quality_passed'], result
    report['static_result'] = result
    registry.create_reference(root / 'mutable')
    registration = registry.register(root, root / 'mutable')
    candidate = root / 'candidates' / registration['candidate_digest']
    report['mutable_cases'] = []
    for scenario in ('basic', 'kill_merge_before_manifest'):
        result = StoreCase(candidate, movie_bundle(), root / scenario, scenario=scenario).run()
        assert result['quality_passed'], result.get('error')
        report['mutable_cases'].append({'scenario': scenario, 'quality_passed': True})
    report['status'] = 'passed'
except BaseException as error:
    report.update(status='failed', reason_code=getattr(error, 'code', type(error).__name__), error=str(error)[:2000])
    raise
finally:
    save(root / 'summary.json', report)
    print(json.dumps(report), flush=True)
