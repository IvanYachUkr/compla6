#!/usr/bin/env python3
"""Recheck frozen public pilot sources under the installed evaluator; no model search."""
import argparse
import json
from pathlib import Path

from compression_lab.engine import Engine
from compression_lab.util import save

parser = argparse.ArgumentParser(description=__doc__)
for key in ('workspace', 'dataset', 'baseline-source', 'candidate-source'):
    parser.add_argument('--' + key, type=Path, required=True)
parser.add_argument('--candidate-name', default='luna-template-final')
args = parser.parse_args()
engine = Engine.init(args.workspace, args.dataset)
report = {'model_calls': 0, 'scope': 'Frozen public Hadoop trial recheck, no held-out claim', 'results': []}
for name, path, track in [('zstd-1-dict', args.baseline_source, 'baseline'),
                          (args.candidate_name, args.candidate_source, 'agent')]:
    registered = engine.register(path, track)
    result = engine.evaluate(registered['candidate_digest'], 'full', track, True)
    raw = engine.raw(result['metrics']['result_id'])
    exported = engine.export(result['metrics']['result_id'], engine.root / 'exports' / (name + '.zip'))
    report['results'].append({'name': name, 'result': result, 'raw': raw, 'export': exported})
    save(engine.root / 'pilot-recheck.json', report)
    print(json.dumps({'name': name, 'status': result['status'], 'quality_passed': raw['quality_passed'],
                      'result_id': result['metrics']['result_id']}), flush=True)
    if not raw['quality_passed']:
        raise RuntimeError('Frozen public pilot no longer passes quality gates')
report['state'] = engine.state()
report['comparison'] = engine.compare([r['result']['metrics']['result_id'] for r in report['results']])
report['resume'] = engine.resume()
report['status'] = 'complete'
save(engine.root / 'pilot-recheck.json', report)
