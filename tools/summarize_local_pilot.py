#!/usr/bin/env python3
"""Derive the public pilot table from preserved raw evaluator records."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--evidence', type=Path, required=True)
args = parser.parse_args()
root = args.evidence


def read(name):
    return json.loads((root / name).read_text())


def row(name, result_id, raw, phase):
    account = raw.get('accounting', {})
    actual = account.get('all', {}).get('actual', {})
    projected = account.get('development', {}).get('projection') or {}
    timing = raw.get('timing', {})
    return {'phase': phase, 'name': name, 'result_id': result_id, 'status': raw['status'],
            'quality_passed': raw.get('quality_passed', False), 'depth': raw['depth'],
            'candidate_digest': raw['candidate_digest'], 'runtime_digest': raw['runtime_digest'],
            'archive_bytes': actual.get('archive_bytes'),
            'historical_primary_bytes': actual.get('historical_primary_bytes'),
            'deployment_actual_bytes': actual.get('deployment_total_bytes'),
            'development_projected_deployment_bytes': projected.get('deployment_total_bytes'),
            'encode_MBps': timing.get('encode', {}).get('median_decimal_MB_per_second'),
            'decode_MBps': timing.get('decode', {}).get('median_decimal_MB_per_second'),
            'reason_codes': raw.get('reason_codes', [])}


baseline = read('hadoop-baselines.json')
raw = read('hadoop-baseline-raw.json')['results']
rows = [row(value['name'], value['result_id'], raw[value['result_id']], 'live-pilot-0.1.1')
        for value in baseline['metrics']['rows']]
agent = read('hadoop-agent-raw.json')
rows += [row('luna-template-' + str(i + 1), value['result_id'], value['raw'], 'live-pilot-0.1.1')
         for i, value in enumerate(agent['results'])]
final = read('hadoop-final-recheck-0.2.1.json')
rows += [row(value['name'], value['result']['metrics']['result_id'], value['raw'], 'final-recheck-0.2.1')
         for value in final['results']]
usage = read('prime-live-luna/status.json')
sources = ['hadoop-baselines.json', 'hadoop-baseline-raw.json', 'hadoop-agent-raw.json',
           'hadoop-final-recheck-0.2.1.json', 'prime-live-luna/status.json']
usage_by_model = {'gpt-5.6-luna': usage['usage']}
if (root / 'sol-final-summary.json').exists():
    sol = read('sol-final-summary.json')
    rows += [row(value['name'], value['result']['metrics']['result_id'], value['raw'], 'sol-high-0.2.1')
             for value in sol['results']]
    usage_by_model['gpt-5.6-sol'] = read('prime-live-sol-high/status.json')['usage']
    sources += ['sol-final-summary.json', 'prime-live-sol-high/status.json']
report = {'schema_version': 1, 'scope': 'Public model pilots, not a held-out model ranking',
          'canonical_bytes': 33977121, 'objects': 469, 'groups': 24,
          'train_objects': 351, 'development_objects': 118,
          'development_bytes': 4356179, 'deployment_horizon_bytes': 100000000,
          'rows': rows, 'model_usage': usage['usage'], 'model_usage_by_model': usage_by_model,
          'usage_note': 'Processed context, including cached input; host cost estimate, not an invoice',
          'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources}}
(root / 'pilot-summary.json').write_text(json.dumps(report, indent=2) + '\n')
with (root / 'pilot-summary.csv').open('w', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(json.dumps({'status': 'complete', 'rows': len(rows), 'output': str(root / 'pilot-summary.json')}))
