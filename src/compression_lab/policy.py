"""One public evidence policy for finish, feedback, brief and recovery.

This reads committed evaluator records; caller-supplied metrics have no authority.
"""
from __future__ import annotations
import math
from .util import Error
from .dataset import encoding_timing_key
from .workloads import bridge


class PublicPolicy:
    def __init__(self, engine):
        if hasattr(engine, 'researcher_view'): engine=engine.researcher_view()
        self.engine = engine
        self.card, self.pin = engine._profile_context()
        self.state = engine.state()
        self._rows = {}; self._verified = set(); self._controls = None

    def _raw(self, result_id):
        if result_id not in self._rows: self._rows[result_id]=self.engine.raw(result_id)
        return self._rows[result_id]

    def _verify(self, candidate_digest):
        if candidate_digest not in self._verified:
            bridge.verify_candidate(self.engine.root/'candidates'/candidate_digest)
            self._verified.add(candidate_digest)

    def _reasons(self, raw, track):
        reasons = []
        expected = {
            'run_id': self.state['run_id'], 'card_digest': self.state['card_digest'],
            'runtime_digest': self.state['runtime_digest'], 'track': track, 'depth': 'full',
            'resource_profile': self.pin['resource_profile']['id'],
            'resource_profile_digest': self.pin['resource_profile_digest'],
            'resource_profile_details': self.pin['resource_profile'],
            'resource_runtime_digest': self.pin['runtime']['runtime_digest'],
            'workload': self.card.get('workload', 'independent_objects'),
            'timing_scope': self.card.get('timing_policy', {}).get('scope', 'train-plus-development-v1'),
            'accounting_policy': self.card.get('accounting_policy', 'standalone-v1'),
        }
        for key, value in expected.items():
            if raw.get(key) != value: reasons.append(key+'_mismatch')
        if 'primary_size_policy' in self.card and raw.get('primary_size_policy') != self.card['primary_size_policy']:
            reasons.append('primary_size_policy_mismatch')
        if 'operation' in self.card.get('timing_policy', {}) and raw.get('timing_operation') != self.card['timing_policy']['operation']:
            reasons.append('timing_operation_mismatch')
        if 'encoding_floor_scope' in self.card.get('timing_policy', {}) and raw.get('encoding_floor_scope') != self.card['timing_policy']['encoding_floor_scope']:
            reasons.append('encoding_floor_scope_mismatch')
        if raw.get('quality_passed') is not True: reasons.append('quality_not_passed')
        if raw.get('eligible') is not True or raw.get('status') != 'eligible': reasons.append('not_eligible')
        if raw.get('workload') != 'mutable_store':
            speed = raw.get('timing', {}).get(encoding_timing_key(self.card), {}).get('median_bytes_per_second')
            floor = self.card['objective']['encode_floor_bytes_per_second']
            if not isinstance(speed, (float, int)) or isinstance(speed, bool) or not math.isfinite(speed) or speed <= 0 or (floor is not None and speed < floor):
                reasons.append('encode_floor_not_met')
        try:
            cost = bridge.rank_cost(raw)
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
                raise Error('invalid_rank_metric')
        except Error as ex: reasons.append(ex.code)
        return reasons

    def assess(self, result_id, candidate_digest=None):
        raw = self._raw(result_id)
        reasons = self._reasons(raw, 'agent')
        if candidate_digest is not None and raw['candidate_digest'] != candidate_digest:
            reasons.append('candidate_result_mismatch')
        # A digest in a result does not substitute for the retained executable snapshot.
        self._verify(raw['candidate_digest'])
        best = self.best_control()
        try: cost = bridge.rank_cost(raw)
        except Error: cost = None
        qualified = not reasons
        report = {'result_id': result_id, 'candidate_digest': raw['candidate_digest'],
                'candidate_qualified': qualified, 'reason_codes': reasons,
                'package_bytes': cost, 'best_compatible_control': best,
                'baseline_improved': cost < best['package_bytes'] if qualified and best else None,
                'encode_floor_bytes_per_second': self.card['objective']['encode_floor_bytes_per_second'],
                'encoding_floor_scope': self.card.get('timing_policy', {}).get('encoding_floor_scope', 'encode'),
                'resource_profile': self.pin['resource_profile']['id']}
        if self.card.get('baseline_visibility','visible')=='hidden':
            report.pop('best_compatible_control')
            report.pop('baseline_improved')
        return report

    def best_control(self):
        if self.card.get('baseline_visibility','visible')=='hidden': return None
        if self._controls is None:
            controls=[]
            for rid in self.state['results']:
                row = self._raw(rid)
                if row.get('track') == 'baseline' and not self._reasons(row, 'baseline'):
                    self._verify(row['candidate_digest'])
                    controls.append({'result_id':rid,'candidate_digest':row['candidate_digest'],
                                     'package_bytes':bridge.rank_cost(row)})
            self._controls=controls
        return min(self._controls,key=lambda x:(x['package_bytes'],x['result_id']),default=None)

    def best(self):
        results = []
        for rid in self.state['results']:
            row = self._raw(rid)
            if row.get('track') == 'agent' and not self._reasons(row, 'agent'):
                assessed = self.assess(rid)
                if assessed['candidate_qualified']: results.append(assessed)
        return min(results, key=lambda x: (x['package_bytes'], x['result_id']), default=None)

    def next_action(self, objective='qualification', closed=False, deadline=False, *, stop_on_success=False):
        best = self.best()
        if closed: return {'command': 'archive', 'result_id': best['result_id'] if best else None}
        if self.state['active_job']: return {'command': 'status', 'job_id': self.state['active_job']}
        success = best and (objective == 'qualification' or best['baseline_improved'] is True)
        budget = self.state['budgets'].get('agent', {})
        exhausted = (budget.get('evaluations', 0) >= self.state['search_budget']['candidate_evaluations']
                     or budget.get('wall_seconds', 0) >= self.state['search_budget']['wall_seconds'])
        if (stop_on_success and success) or exhausted or deadline:
            return {'command': 'finish', 'outcome': 'success' if success else 'negative',
                    'candidate_digest': best['candidate_digest'] if best else None,
                    'result_id': best['result_id'] if best else None}
        return {'command': 'research', 'full_required_for_success': True}
