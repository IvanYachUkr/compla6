#!/usr/bin/env python3
"""Fresh synthetic full-gate verification; never a research performance claim."""
import argparse
import json
from pathlib import Path
import zipfile

from compression_lab import dataset, native_baselines
from compression_lab.engine import Engine
from compression_lab.util import Error, load, save, sha
from compression_lab.workloads.bridge import rank_cost
from prepare_agent import prepare


def verify(output):
    output.mkdir(parents=True)
    inputs = output/'inputs'; inputs.mkdir()
    (inputs/'text').write_bytes(b'clean native compression; repeated patterns\n'*16000)
    (inputs/'bytes').write_bytes(bytes(range(256))*1024)
    card_path = dataset.create_research_input(output/'data', [inputs/'text',inputs/'bytes'], 'run-contract-canary', implementation='open', smoke=True)
    engine = Engine.init(output/'workspace', card_path)
    prepare(engine.root)
    card, rows = engine.metadata()
    source = native_baselines.create(engine.root/'workbench/canary', 'zstd', 1, rows, True,
        max_threads=1, fitting_partition='corpus', offline_replay=True)
    try:
        engine.register(source)
    except Error as error:
        assert error.code == 'hypothesis_required', error
    else:
        raise AssertionError('Missing hypothesis was admitted')
    hypothesis = engine.record_hypothesis('A conventional native codec preserves both complete byte streams',
        'Small archive with bounded memory', 'Any byte mismatch, missing timing trial or inconsistent score')['metrics']
    manifest = load(source/'candidate.json')
    manifest.setdefault('hypothesis', {})['experiment_id'] = hypothesis['experiment_id']
    save(source/'candidate.json', manifest)
    registration = engine.register(source)
    result = engine.evaluate(registration['candidate_digest'], 'full', wait=True)
    assert result['metrics']['quality_passed'], result
    result_id = result['metrics']['result_id']; raw = engine.raw(result_id)
    n = sum(path.stat().st_size for path in inputs.iterdir())
    assert raw['timing_scope'] == 'whole-dataset-v1'
    assert raw['timing_operation'] == 'offline-plus-online-v1'
    assert raw['encoding_floor_scope'] == 'combined'
    assert raw['primary_size_policy'] == 'standard-codec-available-v1'
    for phase in ('offline', 'encode', 'combined', 'decode'):
        assert len(raw['timing_trials'][phase]) == 7
        assert all(row['canonical_bytes'] == n for row in raw['timing_trials'][phase])
    for offline, online, combined in zip(*(raw['timing_trials'][phase] for phase in ('offline','encode','combined'))):
        assert offline['elapsed_ns'] > 0
        assert combined['elapsed_ns'] == offline['elapsed_ns']+online['elapsed_ns']
    primary = raw['reported_accounting']['corpus']
    assert rank_cost(raw) == primary['actual']['deployment_total_bytes']
    feedback = engine.feedback(result_id)['metrics']
    assert feedback['corpus'] == primary
    stages = feedback['encoding_stages']
    assert stages['offline'] == raw['timing']['offline']
    assert stages['online'] == raw['timing']['encode']
    assert stages['combined'] == raw['timing']['combined']
    exported = output/'candidate.zip'; engine.export(result_id, exported)
    with zipfile.ZipFile(exported) as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read('evidence/hypothesis.json')) == hypothesis
        assert json.loads(archive.read('evidence/raw.json'))['timing_trials'] == raw['timing_trials']
    # A data-independent codec must report zero preparation and equal total/online trials.
    plain = native_baselines.create(engine.root/'workbench/unfitted', 'zstd', 1, max_threads=1)
    plain_manifest = load(plain/'candidate.json'); plain_manifest['hypothesis']['experiment_id'] = hypothesis['experiment_id']
    save(plain/'candidate.json', plain_manifest)
    plain_registration = engine.register(plain)
    plain_result = engine.evaluate(plain_registration['candidate_digest'], 'full', wait=True)
    assert plain_result['metrics']['quality_passed'], plain_result
    plain_raw = engine.raw(plain_result['metrics']['result_id'])
    assert plain_raw['timing']['offline']['used'] is False and plain_raw['timing']['offline']['median_seconds'] == 0
    assert not plain_raw['timing_trials']['offline']
    assert [row['elapsed_ns'] for row in plain_raw['timing_trials']['combined']] == [row['elapsed_ns'] for row in plain_raw['timing_trials']['encode']]
    engine.export(plain_result['metrics']['result_id'], output/'unfitted-candidate.zip')
    summary = {'status':'passed', 'evidence_class':'synthetic full-gate integration, not performance qualification',
        'model_calls':0, 'result_id':result_id, 'candidate_digest':registration['candidate_digest'],
        'canonical_bytes':n, 'quality_passed':raw['quality_passed'], 'eligible':raw['eligible'],
        'reason_codes':raw['reason_codes'], 'gates':raw['gates'], 'timing':raw['timing'],
        'timing_operation':raw['timing_operation'], 'primary_size_policy':raw['primary_size_policy'],
        'encoding_floor_scope':raw['encoding_floor_scope'], 'encoding_stages':stages,
        'unfitted_check':{'result_id':plain_result['metrics']['result_id'], 'quality_passed':plain_raw['quality_passed'], 'timing':plain_raw['timing']},
        'primary_accounting':primary, 'hypothesis':hypothesis, 'export_sha256':sha(exported)}
    save(output/'SUMMARY.json', summary)
    print(json.dumps({key:summary[key] for key in ('status','result_id','quality_passed','eligible','model_calls')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',required=True,type=Path)
    verify(parser.parse_args().output)
