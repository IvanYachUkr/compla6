"""Controller invariants, using committed-evidence fixtures rather than codecs."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab.util import Error, digest
from compression_lab.controller import Controller
from compression_lab.policy import PublicPolicy


class EvidenceEngine:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.card = {'_primary_profile':'primary','objective': {'encode_floor_bytes_per_second': 100},
                     'timing_policy': {'scope': 'development-v1'},
                     'accounting_policy': 'supervisor-decoder-v1',
                     'workload': 'independent_objects'}
        self.pin = {'resource_profile': {'id': 'primary'}, 'resource_profile_digest': 'pd',
                    'runtime': {'runtime_digest': 'runtime'}}
        self.s = {'run_id': 'run-test', 'card_digest': 'card', 'runtime_digest': 'runtime',
                  'stage': 'public_search', 'active_job': None, 'results': [],
                  'budgets': {'agent': {'evaluations': 0, 'wall_seconds': 0}},
                  'search_budget': {'candidate_evaluations': 3, 'wall_seconds': 60}}
        self.rows = {}

    def state(self): return copy.deepcopy(self.s)
    def metadata(self): return copy.deepcopy(self.card), []
    def _profile_context(self, profile=None): return copy.deepcopy(self.card), copy.deepcopy(self.pin)
    def raw(self, rid):
        if rid not in self.s['results']: raise Error('result_not_committed')
        return copy.deepcopy(self.rows[rid])

    def add(self, rid='r-agent', **kw):
        raw = {'run_id': 'run-test', 'candidate_digest': 'candidate-a', 'card_digest': 'card',
               'runtime_digest': 'runtime', 'track': 'agent', 'depth': 'full',
               'status': 'eligible', 'eligible': True, 'quality_passed': True,
               'workload': 'independent_objects', 'resource_profile': 'primary',
               'resource_profile_digest': 'pd', 'resource_profile_details': {'id': 'primary'},
               'resource_runtime_digest': 'runtime', 'timing_scope': 'development-v1',
               'accounting_policy': 'supervisor-decoder-v1',
               'timing': {'encode': {'median_bytes_per_second': 120}},
               'decoder_accounting': {'development': {'actual': {'deployment_total_bytes': 500}}}}
        raw.update(kw)
        self.rows[rid] = raw
        self.s['results'].append(rid)
        return rid


def protocol():
    return {'schema_version': 1, 'objective': 'qualification', 'deadline_epoch': time.time()+600,
            'max_children': 1, 'max_depth': 1,
            'models': {'root': [{'model': 'provider/root', 'effort': 'medium'}],
                       'worker': [{'model': 'provider/worker', 'effort': 'high'}]},
            'max_feedback_attempts': 2}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.e = EvidenceEngine(self.tmp.name)
        self.verify = patch('compression_lab.policy.bridge.verify_candidate', return_value=({}, {}))
        self.verify.start(); self.addCleanup(self.verify.stop)
        self.c = Controller.configure(self.e, protocol())
        self.c.attach_backend('fixture', '1')
        self.c.admit('root-launch', 'root', None, 'provider/root', 'medium')
        self.c.bind_session('root', 'native-root', 'provider/root', 'medium')

    def test_qualified_or_improved_result_keeps_research_open_by_default(self):
        for objective in ('qualification', 'improvement'):
            with self.subTest(objective=objective):
                engine = EvidenceEngine(self.e.root/objective)
                controller = Controller.configure(engine, {**protocol(), 'objective':objective})
                engine.add('r-baseline', track='baseline', decoder_accounting={
                    'development':{'actual':{'deployment_total_bytes':600}}})
                engine.add()
                view = controller.brief()
                self.assertEqual(view['next_action']['command'], 'research')
                self.assertNotIn('stop_on_success', view)
                self.assertTrue(view['best_candidate']['candidate_qualified'])

    def test_owner_can_explicitly_enable_early_success_stopping(self):
        engine = EvidenceEngine(self.e.root/'opt-in')
        try:
            controller = Controller.configure(engine, {**protocol(), 'stop_on_success':True})
        except Error as error:
            self.fail('Explicit stop_on_success must be supported: '+str(error))
        engine.add()
        view = controller.brief()
        self.assertEqual(view['next_action']['command'], 'finish')
        self.assertEqual(view['next_action']['outcome'], 'success')
        self.assertIs(view['stop_on_success'], True)

    def test_false_and_invalid_completion_opt_in(self):
        engine = EvidenceEngine(self.e.root/'opt-out')
        controller = Controller.configure(engine, {**protocol(), 'stop_on_success':False})
        engine.add()
        self.assertEqual(controller.brief()['next_action']['command'], 'research')
        self.assertNotIn('stop_on_success', controller.brief())
        for value in ('false', 1, None):
            with self.assertRaisesRegex(Error, 'invalid_stop_policy'):
                Controller.configure(EvidenceEngine(self.e.root/str(value)), {**protocol(), 'stop_on_success':value})

    def test_slow_offline_preparation_does_not_fail_the_online_floor(self):
        self.e.card['timing_policy'].update(operation='offline-plus-online-v1', encoding_floor_scope='online')
        self.e.add(timing_operation='offline-plus-online-v1', encoding_floor_scope='online', timing={
            'encode':{'median_bytes_per_second':120}, 'offline':{'median_seconds':600},
            'combined':{'median_bytes_per_second':1}})
        self.assertTrue(PublicPolicy(self.e).assess('r-agent')['candidate_qualified'])
        self.e.rows['r-agent']['encoding_floor_scope'] = 'combined'
        self.assertIn('encoding_floor_scope_mismatch', PublicPolicy(self.e).assess('r-agent')['reason_codes'])

    def test_success_requires_exact_primary_full_agent_evidence(self):
        for change in ({'track':'baseline'}, {'depth':'screen'}, {'depth':'quick'},
                       {'resource_profile':'reference'}, {'timing_scope':'old'},
                       {'accounting_policy':'other'}, {'eligible':False}, {'quality_passed':False},
                       {'status':'failed'}, {'timing':{'encode':{'median_bytes_per_second':99}}}):
            rid = self.e.add('r-'+digest(change)[:12], **change)
            receipt = self.c.finish(rid, 'candidate-a', rid, 'success')
            self.assertFalse(receipt['accepted'], change)
        self.e.add()
        wrong = self.c.finish('wrong', 'candidate-other', 'r-agent', 'success')
        self.assertFalse(wrong['accepted'])
        good = self.c.finish('good', 'candidate-a', 'r-agent', 'success')
        self.assertTrue(good['accepted'])
        self.assertTrue(good['candidate_qualified'])

    def test_final_trial_success_survives_exhaustion_and_export_is_not_finish(self):
        self.e.add()
        self.e.s['budgets']['agent']['evaluations'] = 3
        view = self.c.brief()
        self.assertEqual(view['next_action']['command'], 'finish')
        receipt = self.c.stop('evaluation_budget')
        self.assertTrue(receipt['candidate_qualified'])
        self.assertEqual(receipt['stop_reason'], 'evaluation_budget')

    def test_finish_request_is_durable_and_idempotent(self):
        self.e.add()
        a = self.c.finish('finish-1', 'candidate-a', 'r-agent', 'success')
        b = Controller(self.e).finish('finish-1', 'candidate-a', 'r-agent', 'success')
        self.assertEqual(a, b)
        with self.assertRaisesRegex(Error, 'request_id_conflict'):
            self.c.finish('finish-1', 'candidate-a', 'r-agent', 'negative')
        with self.assertRaisesRegex(Error, 'controller_closed'): self.c.guard_research()

    def test_child_limits_models_and_depth_checked_before_admission(self):
        with self.assertRaisesRegex(Error, 'unauthorized_model'):
            self.c.admit('bad', 'bad', 'root', 'provider/worker', 'low')
        self.c.admit('launch-1', 'worker', 'root', 'provider/worker', 'high')
        self.assertEqual(len(self.c.state()['nodes']), 2)
        self.c.admit('launch-1', 'worker', 'root', 'provider/worker', 'high')
        with self.assertRaisesRegex(Error, 'child_limit'):
            self.c.admit('launch-2', 'worker2', 'root', 'provider/worker', 'high')
        with self.assertRaisesRegex(Error, 'depth_limit'):
            self.c.admit('launch-3', 'worker3', 'worker', 'provider/worker', 'high')

    def test_idle_parent_waits_for_child_and_job(self):
        self.c.admit('launch', 'worker', 'root', 'provider/worker', 'high')
        self.c.observe('idle-1', 'root', 'idle', 1)
        self.assertEqual(self.c.brief()['lifecycle'], 'waiting')
        self.assertEqual(self.c.pending_deliveries(), [])
        self.e.add()
        self.assertFalse(self.c.finish('early', 'candidate-a', 'r-agent', 'success')['accepted'])
        self.c.observe('child-done', 'worker', 'completed', 1)
        self.e.s['active_job'] = 'job-one'
        self.assertEqual(self.c.brief()['next_action']['command'], 'status')
        self.e.s['active_job'] = None
        self.c.reconcile()
        pending = self.c.pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['target'], 'root')

    def test_usage_snapshots_count_each_session_once_without_a_token_cap(self):
        self.c.record_usage('root', 'native-root', 2_000_000, 40_000, 1)
        self.c.record_usage('root', 'native-root', 2_000_000, 40_000, 1)
        self.c.record_usage('root', 'native-root', 3_000_000, 50_000, 2)
        self.c.admit('launch', 'worker', 'root', 'provider/worker', 'high')
        self.c.bind_session('worker', 'native-worker', 'provider/worker', 'high')
        self.c.record_usage('worker', 'native-worker', 400_000, 10_000, 1)
        usage = Controller(self.e).brief()['usage']
        self.assertEqual(usage['input_tokens'], 3_400_000)
        self.assertEqual(usage['output_tokens'], 60_000)
        self.assertEqual(usage['total_tokens'], 3_460_000)
        self.c.guard_research()

    def test_usage_snapshot_identity_order_and_corrections(self):
        self.c.record_usage('root', 'native-root', 900, 100, 2)
        self.c.record_usage('root', 'native-root', 100, 10, 1)
        self.assertEqual(self.c.brief()['usage']['total_tokens'], 1000)
        with self.assertRaisesRegex(Error, 'usage_snapshot_conflict'):
            self.c.record_usage('root', 'native-root', 800, 100, 2)
        with self.assertRaisesRegex(Error, 'native_session_mismatch'):
            self.c.record_usage('root', 'wrong-native', 900, 100, 3)
        corrected = self.c.record_usage('root', 'native-root', 800, 100, 3)
        self.assertTrue(corrected['corrected'])
        self.assertEqual(self.c.brief()['usage']['total_tokens'], 900)
        self.c.guard_research()

    def test_unreported_usage_does_not_block_an_eligible_finish(self):
        self.e.add()
        self.c.observe('running', 'root', 'running', 1)
        receipt = self.c.finish('done', 'candidate-a', 'r-agent', 'success')
        self.assertTrue(receipt['accepted'])
        self.assertEqual(receipt['usage_at_finish']['unreported_nodes'], ['root'])

    def test_host_stop_waits_for_the_root_turn_to_drain(self):
        self.c.observe('running', 'root', 'running', 1)
        receipt = self.c.stop('operator_stop')
        self.assertEqual(receipt['status'], 'stopping')
        self.assertEqual(receipt['outstanding']['root_turns'], ['root'])
        self.assertIsNone(self.c.state()['completion'])
        self.c.observe('stopped', 'root', 'stopped', 2)
        self.assertTrue(self.c.state()['completion']['drain_complete'])

    def test_invalid_usage_is_rejected_without_changing_reported_totals(self):
        for field in ('input_tokens', 'output_tokens', 'sequence'):
            for invalid in (-1, True, 1.5):
                values = {'input_tokens': 100, 'output_tokens': 10, 'sequence': 1, field: invalid}
                with self.assertRaisesRegex(Error, 'invalid_' + field):
                    self.c.record_usage('root', 'native-root', **values)
        self.assertEqual(self.c.brief()['usage']['unreported_nodes'], ['root'])

    def test_a_failed_control_scan_cannot_be_reused_as_complete_evidence(self):
        self.e.add('r-first-control', track='baseline')
        self.e.add('r-broken-control', track='baseline')
        original = self.e.raw
        def read(rid):
            if rid == 'r-broken-control': raise Error('corrupt_result')
            return original(rid)
        with patch.object(self.e, 'raw', side_effect=read):
            policy = PublicPolicy(self.e)
            for _ in range(2):
                with self.assertRaisesRegex(Error, 'corrupt_result'):
                    policy.best_control()

    def test_ambiguous_delivery_not_resent_and_ack_survives_restart(self):
        self.c.observe('idle', 'root', 'idle', 1)
        msg = self.c.pending_deliveries()[0]
        self.c.claim_delivery(msg['id'])
        self.assertEqual(Controller(self.e).pending_deliveries(), [])
        self.assertEqual(len(self.c.unacknowledged_deliveries()), 1)
        self.c.ack_delivery(msg['id'], 'native-receipt')
        self.c.ack_delivery(msg['id'], 'native-receipt')
        self.c.reconcile()
        self.assertEqual(self.c.pending_deliveries(), [])

    def test_feedback_bounded_by_new_idle_events_and_exhaustion(self):
        for n in range(5): self.c.observe('idle-'+str(n), 'root', 'idle', n+1)
        self.assertLessEqual(len(self.c.state()['outbox']), 2)
        self.e.s['budgets']['agent']['evaluations'] = 3
        self.c.reconcile()
        self.assertEqual(self.c.brief()['next_action']['command'], 'finish')
        self.assertEqual(self.c.brief()['next_action']['outcome'], 'negative')

    def test_deadline_blocks_new_work_but_retains_evidence(self):
        self.e.add()
        with patch('compression_lab.controller.time.time', return_value=time.time()+1000):
            with self.assertRaisesRegex(Error, 'deadline_reached'): self.c.guard_research()
            self.assertTrue(self.c.stop('deadline')['candidate_qualified'])

    def test_improvement_is_separate_and_requires_compatible_control(self):
        self.e.add()
        self.e.add('r-base', track='baseline')
        p = PublicPolicy(self.e).assess('r-agent', 'candidate-a')
        self.assertTrue(p['candidate_qualified']); self.assertFalse(p['baseline_improved'])
        self.e.rows['r-base']['decoder_accounting']['development']['actual']['deployment_total_bytes'] = 700
        self.assertTrue(PublicPolicy(self.e).assess('r-agent', 'candidate-a')['baseline_improved'])
        self.e.rows['r-base']['resource_profile'] = 'reference'
        self.assertIsNone(PublicPolicy(self.e).assess('r-agent', 'candidate-a')['baseline_improved'])

    def test_stale_native_state_cannot_reopen_completed_child(self):
        self.c.admit('launch', 'worker', 'root', 'provider/worker', 'high')
        self.c.observe('done', 'worker', 'completed', 4)
        self.c.observe('late-running', 'worker', 'running', 3)
        self.assertEqual(self.c.state()['nodes']['worker']['status'], 'completed')

    def test_negative_finish_cannot_erase_already_qualified_evidence(self):
        self.e.add()
        receipt = self.c.finish('negative', outcome='negative')
        self.assertTrue(receipt['accepted'])
        self.assertTrue(receipt['candidate_qualified'])
        self.assertEqual(receipt['evidence']['result_id'], 'r-agent')

    def test_unattached_backend_cannot_admit_a_session(self):
        second = EvidenceEngine(Path(self.tmp.name)/'second')
        pending = Controller.configure(second, protocol())
        with self.assertRaisesRegex(Error,'backend_not_attached'):
            pending.admit('launch','root',None,'provider/root','medium')
        self.assertEqual(pending.state()['nodes'],{})

    def test_broken_evidence_does_not_erase_usage_or_block_stop(self):
        self.e.add()
        with patch('compression_lab.policy.bridge.verify_candidate',side_effect=Error('stale_source_digest')):
            self.c.record_usage('root','native-root',60,20,1)
            stopped=self.c.stop('operator_stop')
        self.assertEqual(self.c.brief()['usage']['total_tokens'],80)
        self.assertEqual(self.c.state()['lifecycle'],'closed')
        self.assertEqual(stopped['evidence_error'],'stale_source_digest')

    def test_missing_evidence_produces_a_durable_rejection(self):
        self.e.add()
        with patch.object(self.e,'raw',side_effect=FileNotFoundError('missing public artifact')):
            rejected=self.c.finish('missing','candidate-a','r-agent','success')
        self.assertFalse(rejected['accepted'])
        self.assertIn('evidence_unavailable',rejected['reason_codes'])
        self.assertEqual(self.c.finish('missing','candidate-a','r-agent','success'),rejected)

    def test_explicit_failed_result_does_not_hide_a_qualified_candidate(self):
        self.e.add()
        self.e.add('r-failed',eligible=False,quality_passed=False,status='failed')
        receipt=self.c.finish('negative','candidate-a','r-failed','negative')
        self.assertTrue(receipt['accepted'])
        self.assertTrue(receipt['candidate_qualified'])
        self.assertFalse(receipt['requested_result']['candidate_qualified'])
        self.assertEqual(receipt['evidence']['result_id'],'r-agent')

    def test_stop_blocks_new_admission_but_accepts_final_usage(self):
        self.c.stop('operator_stop')
        with self.assertRaisesRegex(Error,'controller_closed'):
            self.c.admit('late','worker','root','provider/worker','high')
        self.c.record_usage('root','native-root',50,10,1)
        self.assertEqual(self.c.brief()['usage']['total_tokens'],60)

    def test_memory_snapshot_is_pinned_and_frozen_mode_rejects_mutations(self):
        second=EvidenceEngine(Path(self.tmp.name)/'frozen')
        frozen=Controller.configure(second,{**protocol(),'refinement_mode':'frozen'})
        self.assertFalse(frozen.memory_state()['changed'])
        with self.assertRaisesRegex(Error,'refinement_snapshot_frozen'):frozen.guard_refinement(mutation=True)
        initial=frozen.state()['protocol']['memory_snapshot']
        with patch('compression_lab.refinement.RefinementStore.snapshot',return_value={**initial,'digest':'changed'}):
            self.assertTrue(frozen.memory_state()['changed'])
            with self.assertRaisesRegex(Error,'refinement_snapshot_frozen'):frozen.guard_refinement()


if __name__ == '__main__': unittest.main()
