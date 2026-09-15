#!/usr/bin/env python3
"""Curated model-free policy replay, with assertions and preserved raw evidence.

From the staging checkout, choose a new output directory on every invocation:
    PYTHONDONTWRITEBYTECODE=1 python3 tools/controller_impact.py --out results/controller-impact/new-run

The command checks every expected outcome. It never overwrites an existing run.
No model, native evaluator, live observer, account or private data is accessed.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import inspect
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from compression_lab.controller import Controller
from compression_lab.policy import PublicPolicy
from compression_lab.util import digest, now, reply
from test_controller import EvidenceEngine, protocol

FIXED_EPOCH = 1893456000.0
FIXED_STAMP = '2030-01-01T00:00:00+00:00'
BEFORE = ROOT / 'provenance' / 'controller-before-policy.py'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def load_before(path=BEFORE):
    """Compile only preserved pure functions, after checking their copy hashes."""
    text = Path(path).read_text()
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    assignments = {node.targets[0].id: node for node in tree.body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    provenance = ast.literal_eval(assignments['SOURCE_PROVENANCE'].value)
    floor = assignments['FLOOR']
    if ast.literal_eval(floor.value) != provenance['FLOOR']['value']:
        raise ValueError('preserved source hash mismatch: FLOOR')
    functions = []
    for name in ('public_decision', 'resume', 'queued_delivery_bookkeeping'):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        if name == 'queued_delivery_bookkeeping':
            code = ''.join(lines[node.body[0].lineno-1:node.body[-1].end_lineno])
            expected = provenance[name]['selected_statements_sha256']
        else:
            code = ''.join(lines[node.lineno-1:node.end_lineno])
            expected = provenance[name]['preserved_sha256']
        if sha(code) != expected:
            raise ValueError('preserved source hash mismatch: '+name)
        functions.append(node)
    namespace = {'reply': reply}
    # The observer's imports, check(), tick(), subprocess and filesystem code
    # are absent. The queue wrapper mutates only its supplied fixture dictionary.
    exec(compile(ast.Module(body=[floor, *functions], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace, provenance


class SyntheticEvidenceEngine(EvidenceEngine):
    """Explicit synthetic trusted inputs; no evaluator or native process exists."""
    def __init__(self, root):
        super().__init__(root)
        self.card['objective']['encode_floor_bytes_per_second'] = 100_000_000
        self.card['timing_policy']['scope'] = 'validation-only-v2'

    def add(self, rid='r-agent', **kw):
        fixture = {'timing_scope': 'validation-only-v2', 'primary_resource_profile': 'primary',
                   'timing': {'encode': {'median_bytes_per_second': 120_000_000,
                                         'median_decimal_MB_per_second': 120.0}}}
        fixture.update(kw)
        super().add(rid, **fixture)
        raw = self.rows[rid]
        self.s.update(candidate_digest=raw['candidate_digest'], result_id=rid)
        if raw.get('eligible') is True:
            self.s['best_eligible_result'] = rid
        return rid


def sources():
    paths = {'compression_lab/controller.py': ROOT/'src/compression_lab/controller.py',
             'compression_lab/policy.py': ROOT/'src/compression_lab/policy.py',
             'compression_lab/refinement.py': ROOT/'src/compression_lab/refinement.py',
             'compression_lab/util.py': ROOT/'src/compression_lab/util.py',
             'compression_lab/workloads/bridge.py': ROOT/'src/compression_lab/workloads/bridge.py',
             'tests/test_controller.py': ROOT/'tests/test_controller.py',
             'tools/controller_impact.py': Path(__file__).resolve()}
    result = {name: {'path': str(path.relative_to(ROOT)), 'file_sha256': sha(path.read_text())}
              for name, path in paths.items()}
    for obj, key in ((Controller, 'compression_lab/controller.py'),
                     (PublicPolicy, 'compression_lab/policy.py'),
                     (EvidenceEngine, 'tests/test_controller.py')):
        result[key]['symbol'] = obj.__name__
        result[key]['symbol_source_sha256'] = sha(inspect.getsource(obj))
    return result


def fixture_inputs(engine, controller):
    return {'synthetic': True, 'measured': False, 'fixed_clock_epoch': FIXED_EPOCH,
            'card': copy.deepcopy(engine.card), 'profile_pin': copy.deepcopy(engine.pin),
            'evaluator_state': engine.state(),
            'results': {rid: engine.raw(rid) for rid in engine.state()['results']},
            'controller_state': controller.state()}


def check(case, label, actual, expected):
    row = {'label': label, 'actual': actual, 'expected': expected, 'passed': actual == expected}
    case['assertions'].append(row)


def run_report(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    before, provenance = load_before()
    initial_sources = sources()
    cases = []

    def setup(name):
        engine = SyntheticEvidenceEngine(output/'controller-ledgers'/name)
        controller = Controller.configure(engine, protocol())
        controller.attach_backend('synthetic-fixture', '1')
        controller.admit('root-launch', 'root', None, 'provider/root', 'medium')
        return engine, controller

    def replay(name, engine, controller, rid):
        fixture = fixture_inputs(engine, controller)
        rows = [{**raw, 'result_id': result_id} for result_id, raw in fixture['results'].items()]
        old = {'public_decision': before['public_decision'](engine.state(), rows),
               'resume': before['resume'](engine)}
        view = controller.brief()
        finish = controller.finish('finish-'+name, engine.raw(rid)['candidate_digest'], rid, 'success')
        case = {'id': name, 'fixture': fixture, 'before': old,
                'after': {'brief': view, 'finish': finish}, 'assertions': []}
        cases.append(case)
        return case

    with ExitStack() as stack:
        stack.enter_context(patch('compression_lab.controller.time.time', return_value=FIXED_EPOCH))
        stack.enter_context(patch('compression_lab.controller.now', return_value=FIXED_STAMP))
        stack.enter_context(patch('compression_lab.util.now', return_value=FIXED_STAMP))
        stack.enter_context(patch('compression_lab.policy.bridge.verify_candidate', return_value=({}, {})))

        engine, controller = setup('final_trial_success')
        engine.add()
        engine.s['budgets']['agent']['evaluations'] = engine.s['search_budget']['candidate_evaluations']
        case = replay('final_trial_success', engine, controller, 'r-agent')
        check(case, 'before exits on exhausted budget', case['before']['public_decision']['action'], 'budget_exhausted')
        check(case, 'after keeps last-trial qualification', case['after']['brief']['next_action']['outcome'], 'success')
        check(case, 'after accepts exact finish', case['after']['finish']['accepted'], True)
        check(case, 'after retains qualified evidence', case['after']['finish']['candidate_qualified'], True)

        engine, controller = setup('baseline_is_not_submission')
        engine.add('r-control', track='baseline', candidate_digest='candidate-control')
        case = replay('baseline_is_not_submission', engine, controller, 'r-control')
        check(case, 'legacy resume suggests baseline export', case['before']['resume']['metrics']['next_action'],
              {'command': 'export', 'result': 'r-control', 'eligible': True})
        check(case, 'old acceptance already rejects baseline as submission', case['before']['public_decision']['action'], 'retry')
        check(case, 'after continues research', case['after']['brief']['next_action']['command'], 'research')
        check(case, 'after rejects baseline finish', case['after']['finish']['accepted'], False)
        check(case, 'after names the track mismatch', 'track_mismatch' in case['after']['finish']['reason_codes'], True)

        engine, controller = setup('full_ineligible_continues')
        engine.add('r-ineligible', eligible=False, status='ineligible', reason_codes=['encode_floor_not_met'],
                   timing={'encode': {'median_bytes_per_second': 80_000_000,
                                      'median_decimal_MB_per_second': 80.0}})
        case = replay('full_ineligible_continues', engine, controller, 'r-ineligible')
        check(case, 'legacy resume exports full ineligible result', case['before']['resume']['metrics']['next_action'],
              {'command': 'export', 'result': 'r-ineligible', 'eligible': False})
        check(case, 'after continues research', case['after']['brief']['next_action']['command'], 'research')
        check(case, 'after rejects ineligible success', case['after']['finish']['accepted'], False)

        engine, controller = setup('idle_parent_active_child')
        engine.add()
        controller.admit('worker-launch', 'worker', 'root', 'provider/worker', 'high')
        controller.observe('worker-running', 'worker', 'running', 1)
        controller.observe('root-idle', 'root', 'idle', 1)
        case = replay('idle_parent_active_child', engine, controller, 'r-agent')
        check(case, 'legacy resume suggests export while child runs', case['before']['resume']['metrics']['next_action']['command'], 'export')
        check(case, 'after lifecycle is waiting', case['after']['brief']['lifecycle'], 'waiting')
        check(case, 'after identifies retained child', case['after']['brief']['outstanding']['children'], ['worker'])
        check(case, 'after instructs waiting', case['after']['brief']['next_action']['command'], 'wait')
        check(case, 'after refuses premature finish', 'outstanding_work' in case['after']['finish']['reason_codes'], True)

        engine, controller = setup('feedback_dispatch_acknowledgment')
        fixture = fixture_inputs(engine, controller)
        saved = {'idle_since': FIXED_EPOCH-60}
        before['queued_delivery_bookkeeping'](saved, 'synthetic-feedback')
        old = {'fixture_boundary': 'queue append completed; no native acknowledgment observed',
               'saved_after_queue': saved, 'observed_native_receipt': None}
        controller.observe('root-idle', 'root', 'idle', 1)
        pending = controller.pending_deliveries()
        message_id = pending[0]['id']
        claimed = controller.claim_delivery(message_id)
        reopened = Controller(engine)
        reopened.reconcile()
        after_reopen = {'pending': reopened.pending_deliveries(),
                        'unacknowledged': reopened.unacknowledged_deliveries()}
        reopened.ack_delivery(message_id, 'synthetic-native-receipt')
        reopened.ack_delivery(message_id, 'synthetic-native-receipt')
        acknowledged = Controller(engine).state()['outbox'][message_id]
        case = {'id': 'feedback_dispatch_acknowledgment', 'fixture': fixture, 'before': old,
                'after': {'pending': pending, 'claimed': claimed, 'after_reopen': after_reopen,
                          'after_ack_reopen': acknowledged}, 'assertions': []}
        cases.append(case)
        check(case, 'old queue bookkeeping labels delivery before acknowledgment', saved.get('delivered'), ['synthetic-feedback'])
        check(case, 'after claim is dispatched only', claimed['status'], 'dispatched')
        check(case, 'reopen does not resend ambiguous dispatch', after_reopen['pending'], [])
        check(case, 'reopen retains exactly one unacknowledged delivery', len(after_reopen['unacknowledged']), 1)
        check(case, 'explicit synthetic receipt creates acknowledged state', acknowledged['status'], 'acknowledged')
        check(case, 'acknowledgment is durable and idempotent', acknowledged['attempts'], 1)

    if sources() != initial_sources:
        raise RuntimeError('implementation changed during replay; preserve this directory and rerun into a new one')
    for case in cases:
        case['fixture_sha256'] = digest(case['fixture'])
        case['durable_controller_journal'] = 'controller-ledgers/'+case['id']+'/controller/events.jsonl'
    passed = all(row['passed'] for case in cases for row in case['assertions'])
    report = {'schema_version': 1, 'created_at': now(), 'evidence_kind': 'curated_model_free_regression_replay',
              'model_calls': 0, 'native_benchmarks': 0, 'measured_throughput': False,
              'task_success_rate_estimated': False, 'all_assertions_passed': passed,
              'interpretation': 'Fixed incident-shaped fixtures exercise preserved before-policy code and the real after Controller/PublicPolicy. This is regression evidence, not measured compression, model improvement, task-success rate, or held-out generalization.',
              'boundaries': [
                  'Every Engine row, throughput value, result identity, model identity and native event is synthetic trusted fixture input.',
                  'Candidate snapshot verification is mocked; native executable integrity and measured performance are outside this replay.',
                  'Legacy resume suggests archival actions; export itself was not an explicit success receipt. The old acceptance policy already rejects baseline-as-submission.',
                  'Feedback before replays only two original pure state updates after queue append, not the live observer. After reconstructs Controller from its durable ledger; provider transport, process crashes and acknowledgment loss are not exercised.',
                  'Fixture clock and initial empty refinement snapshots are fixed. No refinement adaptation or memory improvement is measured.',
                  'Five curated incidents provide no sampling frame or denominator for an agent task-success rate.'
              ],
              'reproduce': 'PYTHONDONTWRITEBYTECODE=1 python3 tools/controller_impact.py --out results/controller-impact/NEW_RUN_NAME',
              'provenance': {'before': {'path': str(BEFORE.relative_to(ROOT)), 'file_sha256': sha(BEFORE.read_text()),
                                         'sources': provenance}, 'after': initial_sources}, 'cases': cases}
    with (output/'report.json').open('x') as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
    if not passed:
        raise AssertionError('impact replay expectations failed; see preserved report.json')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='new output directory; existing paths are refused')
    args = parser.parse_args()
    report = run_report(args.out)
    print(json.dumps({'report': str(args.out/'report.json'), 'cases': len(report['cases']),
                      'assertions': sum(len(c['assertions']) for c in report['cases']),
                      'all_assertions_passed': report['all_assertions_passed'], 'model_calls': 0}))
