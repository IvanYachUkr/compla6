"""Cheap search signal, not a truncated certification run or a development score."""
from __future__ import annotations
import hashlib
import shutil
import statistics
import tempfile
import time
from collections import deque
from pathlib import Path
from . import accounting, candidate, dataset, deployment
from .gates import Evaluation as FullEvaluation
from .stream import StreamRecord, pack_stream, ORIGINAL_MAGIC, ARCHIVE_MAGIC
from .util import Error, digest, sha

DEFAULT_POLICY = {'schema_version': 1, 'max_objects': 8,
                  'max_canonical_bytes': 8 * 1024 * 1024, 'seed': 20260907}


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_POLICY):
        raise Error('invalid_screening_policy')
    ranges = {'schema_version': (1, 1), 'max_objects': (1, 64),
              'max_canonical_bytes': (1, 64 * 1024 * 1024), 'seed': (0, 2**32 - 1)}
    if any(type(policy[k]) is not int or not lo <= policy[k] <= hi for k, (lo, hi) in ranges.items()):
        raise Error('invalid_screening_policy')
    return policy


def select_rows(rows, policy=None, *, fitting_partition='train'):
    policy = validate_policy(DEFAULT_POLICY if policy is None else policy)
    # Rank whole training objects and round-robin groups; independent of manifest order.
    def key(value):
        return hashlib.sha256((str(policy['seed']) + '\0' + value).encode()).hexdigest()
    groups = {}
    for row in rows:
        if row['split'] == fitting_partition:
            groups.setdefault(row['group'], []).append(row)
    queues = deque(deque(sorted(group, key=lambda r: (key(r['canonical_sha256'] + '\0' + r['alias']), r['alias'])))
                   for _, group in sorted(groups.items(), key=lambda x: (key(x[0]), x[0])))
    selected, skipped, used = [], [], 0
    while queues and len(selected) < policy['max_objects']:
        group = queues.popleft()
        row = group.popleft()
        if used + row['canonical_bytes'] <= policy['max_canonical_bytes']:
            selected.append(row)
            used += row['canonical_bytes']
        else:
            skipped.append(row['alias'])
        if group:
            queues.append(group)
    if not selected:
        raise Error('screen_no_whole_object_fits', f'screen_no_whole_object_fits: No complete {fitting_partition} object fits the screening cap; use full evaluation. Screening never slices objects.')
    selected.sort(key=lambda r: r['alias'])
    identity = [{k: r[k] for k in ('alias', 'group', 'canonical_bytes', 'canonical_sha256')} for r in selected]
    return selected, {'schema_version': 1, 'scope': 'train-only-whole-objects' if fitting_partition == 'train' else 'corpus-whole-objects', 'policy': dict(policy),
        'fitting_partition': fitting_partition,
        'selection_digest': digest({'policy': policy, 'objects': identity}), 'objects': identity,
        'canonical_bytes': used, 'covered_groups': sorted({r['group'] for r in selected}),
        ('training_groups' if fitting_partition == 'train' else 'corpus_groups'): len(groups), 'skipped_for_byte_cap': skipped,
        'not_certification': True, 'development_bytes_used': 0}


class Evaluation(FullEvaluation):
    def run(self):
        self.raw.update(screen_passed=False, quality_passed=False, eligible=False,
                        reason_codes=['screen_not_promotable'])
        try:
            part = dataset.fitting_partition(self.card)
            rows, selection = select_rows(self.rows, self.card.get('screening_policy'), fitting_partition=part)
            self.raw['screening'] = selection
            with tempfile.TemporaryDirectory(prefix='compression-lab-screen-') as td:
                self.tmp = Path(td)
                payloads = []
                for row in rows:
                    payload = Path(row['source']).read_bytes()
                    if len(payload) != row['canonical_bytes'] or hashlib.sha256(payload).hexdigest() != row['canonical_sha256']:
                        raise Error('stale_data_digest', row['alias'])
                    payloads.append(StreamRecord(row['alias'], payload))
                public = tuple(payloads)
                self.raw['screening']['data_integrity_scope'] = f'Selected {part} bytes verified; full evaluation independently verifies every public payload.'
                encoded = self.exact(public, 'screen-' + part)
                self.gate('screen_' + part + '_exact', len(rows))
                # These are smoke bytes only. Full fixtures, parity, sanitizers and adversarial
                # corruption checks remain exclusively in the independent full evaluator.
                if self.card['adapter'] == 'bytes':
                    smoke = tuple(StreamRecord('screen-' + str(i), b) for i, b in enumerate(
                        (b'', b'\0', bytes(range(256)), b'\r\n\x00\xff', b'x' * 4097)))
                    self.exact(smoke, 'screen-smoke-bytes')
                    self.gate('screen_byte_smoke', len(smoke))
                inp, arc = self.tmp/'screen.hbi', self.tmp/'screen.hba'
                inp.write_bytes(pack_stream(ORIGINAL_MAGIC, public))
                arc.write_bytes(pack_stream(ARCHIVE_MAGIC, encoded))
                timing = {}
                for phase, op, file, expected in (
                    ('encode', 'encode_stream', inp, sha(arc)),
                    ('decode', 'decode_stream', arc, sha(inp))):
                    trials = []
                    for trial in range(3):
                        output, _, measurement = self.invoke(op, stream=file)
                        actual = sha(output)
                        trials.append({**measurement, 'trial': trial+1,
                                       'canonical_bytes': selection['canonical_bytes'], 'output_sha256': actual})
                        self.raw['timing_trials'][phase] = trials
                        self.save()
                        if actual != expected:
                            raise Error('nondeterministic_screen_output')
                        shutil.rmtree(output.parent)
                    speeds = [selection['canonical_bytes'] * 1e9 / t['elapsed_ns'] for t in trials]
                    med = statistics.median(speeds)
                    timing[phase] = {'median_decimal_MB_per_second': med/1e6,
                        'median_bytes_per_second': med, 'trials': 3,
                        'relative_MAD': statistics.median(abs(s-med) for s in speeds)/med if med else 0,
                        'scope': part + ' sample only; fresh process, sandbox bootstrap and I/O; diagnostic, not gated'}
                fixed = candidate.costs(self.m, self.reg)
                self.raw['fixed_costs'] = fixed
                sampled_cost = accounting.costs(selection['canonical_bytes'], arc.stat().st_size,
                    fixed, self.card['objective']['deployment_canonical_bytes'])
                # No development/all accounting and no certified timing fields: cannot be
                # accidentally ranked by old consumers looking for those specific keys.
                self.raw['screening'].update(timing=timing, sample_costs=sampled_cost,
                    deployment_views=deployment.views(fixed, {part + '_sample': sampled_cost}, self.card.get('runtime_scenarios')),
                    omitted_full_gates=['clean_rebuild', 'directory_parity', 'independence',
                                       'full_fixtures', 'corruption', 'sanitizers', 'seven_trial_timing'])
                self.raw.update(status='ineligible', screen_passed=True)
        except Exception as exc:
            self.raw.update(status='cancelled' if getattr(exc,'code',None)=='cancelled' else 'failed', error=str(exc)[:2000],
                            reason_codes=['screen_not_promotable', getattr(exc, 'code', 'screen_failed')])
        self.raw['wall_seconds'] = time.perf_counter() - self.started
        self.raw['gate_digest'] = digest({'candidate': self.reg['candidate_digest'],
                                         'depth': 'screen', 'screening': self.raw.get('screening'),
                                         'gates': self.raw['gates']})
        self.save()
        return self.raw


def summary(raw):
    """Transport view; retain full sampling provenance and costs only in raw evidence."""
    screen = raw.get('screening')
    if screen is None:
        return None
    costs = screen.get('sample_costs', {})
    part = screen.get('fitting_partition', 'train')
    group_key = 'training_groups' if part == 'train' else 'corpus_groups'
    return {'schema_version': 1, 'scope': screen['scope'], 'selection_digest': screen['selection_digest'],
            'fitting_partition': part,
            'objects': len(screen['objects']), 'canonical_bytes': screen['canonical_bytes'],
            'covered_groups': len(screen['covered_groups']), group_key: screen[group_key],
            'development_bytes_used': 0, 'not_certification': True, 'timing': screen.get('timing'),
            'sample_archive_ratio': costs.get('archive_ratio'), 'sample_actual': costs.get('actual'),
            'projected_costs_are_extrapolated_from_' + part + '_sample': costs.get('projection'),
            'full_provenance_artifact': 'raw.json'}
