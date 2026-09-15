"""A failed final answer must not end research while commissioned limits remain."""
import tempfile
import time
import unittest
from unittest.mock import patch

from compression_lab.controller import Controller
from compression_lab.util import Error
from test_controller import EvidenceEngine, protocol


class CompletionLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = EvidenceEngine(self.tmp.name)
        self.verify = patch('compression_lab.policy.bridge.verify_candidate', return_value=({}, {}))
        self.verify.start(); self.addCleanup(self.verify.stop)

    def configure(self, **limits):
        c = Controller.configure(self.engine, {**protocol(), 'schema_version': 2,
            'deadline_epoch': None, 'max_feedback_attempts': None,
            'max_failure_continuations': 3, **limits})
        c.attach_backend('fixture', '1')
        c.admit('root-launch', 'root', None, 'provider/root', 'medium')
        c.bind_session('root', 'native-root', 'provider/root', 'medium')
        return c

    def test_unsuccessful_finish_cannot_bypass_remaining_limits(self):
        c = self.configure()
        self.engine.add(eligible=False, timing={'encode': {'median_bytes_per_second': 42}})
        for n in range(5):
            receipt = c.finish('early-'+str(n), outcome='negative')
            self.assertFalse(receipt['accepted'])
            self.assertIn('completion_budget_remaining', receipt['reason_codes'])
        self.assertEqual(c.state()['failure_continuations'], 0)
        self.assertFalse(c.finish('self-blocked', outcome='infrastructure_blocked')['accepted'])

    def test_exactly_three_additional_rounds_survive_restarts_and_allow_full_last_round(self):
        c = self.configure()
        for n in range(3):
            c.observe('idle-'+str(n), 'root', 'idle', n*2)
            c.reconcile(); c.reconcile()
            self.assertEqual(len(c.pending_deliveries()), 1)
            delivery = c.claim_delivery(c.pending_deliveries()[0]['id'])
            c.ack_delivery(delivery['id'], 'native-'+str(n))
            c.observe('running-'+str(n), 'root', 'running', n*2+1)
            c.guard_research()  # The third continuation must be able to evaluate.
            c = Controller(self.engine)
        c.observe('last-idle', 'root', 'idle', 6)
        self.assertEqual(c.state()['failure_continuations'], 3)
        self.assertEqual(c.state()['lifecycle'], 'closed')
        self.assertEqual(c.state()['completion']['stop_reason'], 'attempt_limit')
        self.assertFalse(c.state()['completion']['candidate_qualified'])

    def test_any_configured_limit_can_stop_and_money_requires_host_evidence(self):
        c = self.configure(max_cost_nano=10_000)
        with self.assertRaisesRegex(Error, 'spending_unavailable'): c.guard_research()
        c.record_spending(9_000, 1, False)
        c.guard_research()
        c.record_spending(10_000, 2, False)
        self.assertEqual(c.reconcile()['completion']['stop_reason'], 'money_limit')

    def test_reservation_refusal_stops_even_below_nominal_dollar_limit(self):
        c = self.configure(max_cost_nano=10_000)
        c.record_spending(9_000, 1, True)
        self.assertEqual(c.reconcile()['completion']['stop_reason'], 'money_limit')

    def test_fully_reserved_inflight_request_is_allowed_to_settle(self):
        c = self.configure(max_cost_nano=10_000)
        c.record_spending(10_000, 1, False, pending_requests=1)
        self.assertNotEqual(c.reconcile()['lifecycle'], 'closed')
        c.record_spending(8_000, 2, False)
        c.guard_research()
        self.assertIsNone(c.brief()['completion_limits']['exhausted_reason'])

    def test_money_receipts_reject_stale_conflicts_and_count_no_native_placeholders(self):
        c = self.configure(max_cost_nano=10_000)
        c.record_spending(9_000, 2, False)
        c.record_spending(0, 1, False)
        with self.assertRaisesRegex(Error, 'spending_snapshot_conflict'):
            c.record_spending(0, 2, False)
        self.assertEqual(c.brief()['completion_limits']['spending']['committed_nano'], 9_000)

    def test_time_only_and_attempts_only_and_money_only_are_valid(self):
        for limits in ({'deadline_epoch': time.time()+600},
                       {'max_failure_continuations': 0}, {'max_cost_nano': 10_000}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as root:
                Controller.configure(EvidenceEngine(root), {**protocol(), 'schema_version': 2,
                    'deadline_epoch': None, 'max_feedback_attempts': None, **limits})

    def test_duration_is_resolved_once_and_does_not_reset_on_restart(self):
        c = self.configure(time_limit_seconds=60, max_failure_continuations=None)
        deadline=c.state()['protocol']['deadline_epoch']
        with patch('compression_lab.controller.time.time',return_value=deadline+1):
            restored=Controller(self.engine)
            self.assertEqual(restored.state()['protocol']['deadline_epoch'],deadline)
            self.assertEqual(restored.reconcile()['completion']['stop_reason'],'deadline')

    def test_no_limit_is_invalid_and_qualified_final_trial_is_preserved(self):
        with self.assertRaisesRegex(Error, 'completion_limit_required'):
            self.configure(max_failure_continuations=None)
        c = self.configure(max_failure_continuations=0)
        self.engine.add()
        c.observe('final', 'root', 'idle', 0)
        self.assertTrue(c.state()['completion']['candidate_qualified'])
        self.assertEqual(c.state()['completion']['evidence']['result_id'], 'r-agent')

    def test_active_lab_job_does_not_use_a_continuation(self):
        c = self.configure()
        self.engine.s['active_job'] = 'job-in-progress'
        c.observe('waiting', 'root', 'idle', 0)
        self.assertEqual(c.pending_deliveries(), [])
        self.assertEqual(c.state()['failure_continuations'], 0)

    def test_host_recovery_retains_contract_charges_counters_and_stop_receipt(self):
        c = self.configure(max_cost_nano=10_000)
        c.record_spending(4_000, 1)
        c.observe('idle-0', 'root', 'idle', 0)
        delivery = c.claim_delivery(c.pending_deliveries()[0]['id'])
        c.ack_delivery(delivery['id'], 'native-delivery')
        c.observe('working', 'root', 'running', 1)
        c.stop('infrastructure_blocked')
        c.observe('host-stopped', 'root', 'stopped', 2)
        before = c.state()
        receipt = c.recover_infrastructure('restore-1', before['protocol_digest'], {'root': 'native-root'})
        after = c.state()
        for key in ('protocol', 'protocol_digest', 'nodes', 'spending', 'outbox',
                    'failure_continuations', 'feedback_attempts', 'run_id', 'card_digest', 'runtime_digest'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['recoveries'][0]['completion'], before['completion'])
        self.assertEqual(after['lifecycle'], 'open')
        self.assertIsNone(after['completion'])
        c.guard_research()
        self.assertEqual(c.recover_infrastructure('restore-1', before['protocol_digest'], {'root': 'native-root'}), receipt)
        self.assertEqual(len(c.state()['recoveries']), 1)

    def rejected_delivery(self):
        c = self.configure(max_cost_nano=10_000)
        c.record_spending(4_000, 1)
        c.observe('idle-0', 'root', 'idle', 0)
        delivery = c.claim_delivery(c.pending_deliveries()[0]['id'])
        c.stop('infrastructure_blocked')
        c.observe('host-stopped', 'root', 'stopped', 1)
        response = {'type': 'response', 'id': delivery['id'], 'command': 'prompt',
                    'success': False, 'error_sha256': 'a'*64}
        return c, delivery, response

    def test_explicit_rejection_can_be_requeued_once_without_resetting_charges(self):
        c, delivery, response = self.rejected_delivery()
        before = c.state()
        with self.assertRaisesRegex(Error, 'recovery_delivery_ambiguous'):
            c.recover_infrastructure('ambiguous', before['protocol_digest'], {'root':'native-root'})
        receipt = c.requeue_rejected_delivery('rejection-1', delivery['id'], 1, 'native-root', response)
        after = c.state()
        for key in ('protocol', 'protocol_digest', 'nodes', 'spending', 'completion',
                    'failure_continuations', 'feedback_attempts', 'run_id', 'card_digest', 'runtime_digest'):
            self.assertEqual(after[key], before[key], key)
        queued = c.pending_deliveries()[0]
        self.assertEqual(queued['message'], delivery['message'])
        self.assertEqual(queued['attempts'], 1)
        self.assertEqual(queued['rejections'][0], receipt)
        self.assertEqual(receipt['dispatched_at'], delivery['dispatched_at'])
        with self.assertRaisesRegex(Error, 'delivery_not_dispatched'):
            c.ack_delivery(delivery['id'], 'invented-receipt')
        c.recover_infrastructure('restore-1', before['protocol_digest'], {'root':'native-root'})
        retried = c.claim_delivery(delivery['id'])
        self.assertEqual(retried['attempts'], 2)
        self.assertEqual(c.state()['failure_continuations'], 2)
        self.assertEqual(c.requeue_rejected_delivery('rejection-1', delivery['id'], 1, 'native-root', response), receipt)
        self.assertEqual(c.state()['outbox'][delivery['id']]['status'], 'dispatched')
        c.stop('infrastructure_blocked')
        with self.assertRaisesRegex(Error, 'rejection_attempt_mismatch'):
            c.requeue_rejected_delivery('stale-review', delivery['id'], 1, 'native-root', response)

    def test_requeue_requires_explicit_matching_rejection_and_drained_native_session(self):
        c, delivery, response = self.rejected_delivery()
        for changed in ({'success':True}, {'success':None}, {'id':'other'}, {'command':'get_state'},
                        {'type':'agent_end'}, {'error_sha256':'not-a-hash'}, {'error':'private-text'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(Error, 'invalid_rejection_receipt'):
                c.requeue_rejected_delivery('bad', delivery['id'], 1, 'native-root', {**response, **changed})
        with self.assertRaisesRegex(Error, 'recovery_context_mismatch'):
            c.requeue_rejected_delivery('wrong-root', delivery['id'], 1, 'other-root', response)
        self.assertEqual(c.state()['outbox'][delivery['id']]['status'], 'dispatched')
        c.ack_delivery(delivery['id'], 'native-accepted')
        with self.assertRaisesRegex(Error, 'delivery_not_dispatched'):
            c.requeue_rejected_delivery('accepted', delivery['id'], 1, 'native-root', response)
        c.recover_infrastructure('restore', c.state()['protocol_digest'], {'root':'native-root'})
        with self.assertRaisesRegex(Error, 'recovery_requires_drained_infrastructure_stop'):
            c.requeue_rejected_delivery('active', delivery['id'], 1, 'native-root', response)

    def test_host_recovery_rejects_active_native_work_wrong_identity_and_wrong_contract(self):
        c = self.configure()
        c.observe('working', 'root', 'running', 0)
        c.stop('infrastructure_blocked')
        protocol_digest = c.state()['protocol_digest']
        with self.assertRaisesRegex(Error, 'recovery_requires_drained_infrastructure_stop'):
            c.recover_infrastructure('active', protocol_digest, {'root': 'native-root'})
        c.observe('host-stopped', 'root', 'stopped', 1)
        for key, contract, sessions in (('identity', protocol_digest, {'root': 'replacement'}),
                                        ('contract', 'different', {'root': 'native-root'})):
            with self.subTest(key=key), self.assertRaisesRegex(Error, 'recovery_context_mismatch'):
                c.recover_infrastructure(key, contract, sessions)
        self.assertEqual(c.state()['lifecycle'], 'closed')

    def test_host_recovery_cannot_override_money_or_other_terminal_stops(self):
        c = self.configure(max_cost_nano=10_000)
        c.record_spending(4_000, 1)
        c.stop('infrastructure_blocked')
        c.observe('host-stopped', 'root', 'stopped', 0)
        c.record_spending(10_000, 2)
        with self.assertRaisesRegex(Error, 'recovery_limit_exhausted'):
            c.recover_infrastructure('over-cap', c.state()['protocol_digest'], {'root': 'native-root'})
        for reason in ('operator_stop', 'money_limit', 'attempt_limit', 'deadline', 'evaluation_budget'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                other = Controller.configure(EvidenceEngine(root), protocol())
                other.stop(reason)
                with self.assertRaisesRegex(Error, 'recovery_requires_drained_infrastructure_stop'):
                    other.recover_infrastructure('wrong-reason', other.state()['protocol_digest'], {})


if __name__ == '__main__': unittest.main()
