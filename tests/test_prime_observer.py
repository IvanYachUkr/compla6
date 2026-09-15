"""Offline receipt and shared-lease regressions; never touch a live campaign."""
import copy
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import prime_observer as observer


class ReceiptIdentityTests(unittest.TestCase):
    def test_whole_corpus_result_is_a_qualification_event(self):
        row = {'track': 'agent', 'resource_profile': 'primary', 'primary_resource_profile': 'primary',
               'timing_scope': 'whole-dataset-v1', 'depth': 'full', 'quality_passed': True,
               'eligible': True, 'result_id': 'whole-result',
               'timing': {'encode': {'median_decimal_MB_per_second': 150}}}
        self.assertEqual(observer.public_decision({}, [row])['action'], 'qualified')

    def test_native_child_uses_the_commissioned_effort(self):
        import inspect
        self.assertIn('expected_thinking', inspect.signature(observer.project_native_child).parameters)
        entries = [{'type':'session','id':'child'},
                   {'type':'model_change','provider':'openai-codex','modelId':'gpt-5.6-luna'},
                   {'type':'thinking_level_change','thinkingLevel':'xhigh'},
                   {'type':'message','id':'final','message':{'role':'assistant','stopReason':'stop','content':[]}}]
        observed = observer.project_native_child(entries, {}, expected_thinking='xhigh')
        self.assertIsNone(observed['activity'])
        self.assertNotIn('native_observation_error', observed)

    def test_native_metadata_cannot_escape_the_owned_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); profile = root/'profile'; profile.mkdir()
            outside = root/'outside.jsonl'
            outside.write_text(json.dumps({'type': 'session', 'id': 'root'})+'\n'+
                               json.dumps({'type': 'message', 'id': 'foreign', 'timestamp': 'fixed',
                                           'message': {'role': 'assistant', 'stopReason': 'stop'}})+'\n')
            native_home = root/'virtual-home'
            status = {'verified_session': {'session_id': 'root',
                      'session_file': str(native_home/'../outside.jsonl')}, 'children': {}}
            observed = observer.refresh_native_children(
                {'profile': str(profile), 'native_home': str(native_home)}, status)
            self.assertIsNone(observed['parent_idle_receipt'])
            self.assertIn('native_observation_error', observed)

    def test_result_projection_preserves_feedback_without_retaining_audit_payload(self):
        raw = {'track': 'agent', 'resource_profile': 'primary', 'primary_resource_profile': 'primary',
               'timing_scope': 'validation-only-v2', 'depth': 'full', 'quality_passed': True,
               'eligible': False, 'candidate_digest': 'candidate',
               'reason_codes': ['encoding_below_100_MBps'],
               'timing': {'encode': {'median_decimal_MB_per_second': 98.9}},
               'trial_audit': {'large_details': ['unused evidence']}}
        ledger = {'results': ['result-1'], 'stage': 'public_search'}
        full = [{**raw, 'result_id': 'result-1'}]
        with patch.object(observer, 'read', return_value=raw):
            projected = observer.public_rows(Path('/offline'), ledger)
        self.assertNotIn('trial_audit', projected[0])
        self.assertEqual(observer.public_decision(ledger, projected), observer.public_decision(ledger, full))
        self.assertEqual(observer.continuation_message(ledger, projected),
                         observer.continuation_message(ledger, full))

    def test_native_stop_receipt_requires_last_completed_message(self):
        rows = [{'type': 'session', 'id': 'parent'},
                {'type': 'message', 'id': 'final-1', 'timestamp': 'fixed-time',
                 'message': {'role': 'assistant', 'stopReason': 'stop'}}]
        self.assertEqual(observer.native_idle_receipt(rows),
                         {'session_id': 'parent', 'message_id': 'final-1', 'at': 'fixed-time'})
        for newer in ({'type': 'custom_message', 'customType': 'agent_message'},
                      {'type': 'message', 'message': {'role': 'toolResult'}},
                      {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'error'}}):
            self.assertIsNone(observer.native_idle_receipt(rows+[newer]))

    def test_fresh_terminal_turn_changes_key_but_polling_does_not(self):
        status = {'verified_session': {'session_id': 'parent'},
                  'parent_idle_receipt': {'message_id': 'parent-final-1'},
                  'children': {'child': {'native_idle_receipt': {'message_id': 'child-final-1'}}}}
        decision = {'action': 'baseline_ready'}
        old = observer.feedback_basis(status, decision)
        status['elapsed_seconds'] = 999
        self.assertEqual(observer.feedback_basis(status, decision), old)
        status['parent_idle_receipt']['message_id'] = 'parent-final-2'
        self.assertNotEqual(observer.feedback_basis(status, decision), old)
        newer = copy.deepcopy(observer.feedback_basis(status, decision))
        status['children']['child']['native_idle_receipt']['message_id'] = 'child-final-2'
        self.assertNotEqual(observer.feedback_basis(status, decision), newer)

    def test_lock_probe_never_displaces_an_existing_holder(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            path = Path(tmp)/'benchmark.lock'
            with path.open('w') as holder:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(observer.benchmark_lock_status(path), 'busy')
                fcntl.flock(holder, fcntl.LOCK_UN)
                self.assertEqual(observer.benchmark_lock_status(path), 'free')
            self.assertEqual(observer.benchmark_lock_status(Path(tmp)/'missing'), 'unavailable')


class ObserverReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.driver = self.root/'driver'; self.driver.mkdir()
        self.workspace = self.root/'workspace'
        (self.workspace/'state').mkdir(parents=True)
        (self.workspace/'baseline-campaign').mkdir()
        (self.workspace/'baseline-campaign'/'complete.json').write_text('{}')
        (self.root/'observer').mkdir()
        self.now = 1_700_000_000
        self.write(self.root/'campaign.json', {'deadline_utc': '2030-01-01T00:00:00+00:00',
                   'status': 'research_running', 'workspace': str(self.workspace),
                   'driver_output': str(self.driver), 'profile': str(self.root/'profile')})
        self.write(self.workspace/'state/state.json', {'stage': 'public_search', 'active_job': None,
                   'results': [], 'budgets': {'agent': {'evaluations': 0, 'wall_seconds': 0}},
                   'search_budget': {'candidate_evaluations': 10, 'wall_seconds': 1000}})
        self.status = {'status': 'idle', 'verified_session': {'session_id': 'parent'},
                       'parent_idle_receipt': {'session_id': 'parent', 'message_id': 'parent-final', 'at': 'fixed'},
                       'children': {'child': {'status': 'done', 'activity': None,
                                    'native_idle_receipt': {'session_id': 'child', 'message_id': 'child-final', 'at': 'fixed'}}}}
        self.write(self.driver/'status.json', self.status)
        self.write(self.root/'observer/state.json', {'delivered': ['old-event'], 'idle_since': self.now-91})
        (self.driver/'commands.jsonl').write_text('')
        (self.driver/'events.jsonl').write_text('')
        os.utime(self.driver/'commands.jsonl', (self.now, self.now))
        os.utime(self.driver/'status.json', (self.now, self.now))
        for patcher in (patch.object(observer, 'BASE', self.root),
                        patch.object(observer.time, 'time', return_value=self.now),
                        patch.object(observer, 'refresh_native_children', side_effect=lambda campaign, status: copy.deepcopy(self.status))):
            patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    def tick(self, lock='free'):
        with patch.object(observer, 'benchmark_lock_status', return_value=lock, create=True):
            observer.tick()
        return json.loads((self.root/'observer/status.json').read_text())

    def add_result(self, result_id, eligible=True):
        path = self.workspace/'results'/result_id
        path.mkdir(parents=True)
        self.write(path/'raw.json', {
            'track': 'agent', 'resource_profile': 'primary-four',
            'primary_resource_profile': 'primary-four', 'timing_scope': 'validation-only-v2',
            'depth': 'full', 'quality_passed': True, 'eligible': eligible,
            'candidate_digest': 'candidate-'+result_id,
            'reason_codes': [] if eligible else ['encoding_below_100_MBps'],
            'timing': {'encode': {'median_decimal_MB_per_second': 121.0 if eligible else 99.0}}})
        ledger_path = self.workspace/'state/state.json'
        ledger = json.loads(ledger_path.read_text())
        ledger['results'].append(result_id)
        self.write(ledger_path, ledger)

    def test_successful_evaluation_wakes_parent_once_even_after_a_new_idle_turn(self):
        self.add_result('qualified-1')
        record = self.tick()
        self.assertTrue(record['sent'])
        self.assertEqual(record['decision']['action'], 'evaluation_completed')
        self.assertEqual(record['decision']['result_id'], 'qualified-1')

        command = json.loads((self.driver/'commands.jsonl').read_text())
        (self.driver/'events.jsonl').write_text(json.dumps({
            'type': 'response', 'command': 'prompt', 'id': command['id'], 'success': True})+'\n')
        os.utime(self.driver/'status.json', None)
        self.status['parent_idle_receipt']['message_id'] = 'reviewed-result-final'
        self.status['children']['child']['native_idle_receipt']['message_id'] = 'exported-final'
        repeated = self.tick()
        self.assertTrue(repeated['acknowledged'])
        self.assertFalse(repeated['sent'])
        self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), 1)

        # A later result must still be delivered, even with a prior qualifying result.
        self.add_result('next-result', eligible=False)
        saved = json.loads((self.root/'observer/state.json').read_text())
        saved['idle_since'] = self.now-91
        self.write(self.root/'observer/state.json', saved)
        next_record = self.tick()
        self.assertTrue(next_record['sent'])
        self.assertEqual(next_record['decision']['result_id'], 'next-result')
        self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), 2)

    def test_hidden_run_does_not_wait_for_a_supplied_baseline_marker(self):
        (self.workspace/'baseline-campaign/complete.json').unlink()
        self.write(self.workspace/'dataset-card.json', {'baseline_visibility':'hidden',
                   'timing_policy':{'scope':'validation-only-v2'}})
        self.add_result('own-qualified')
        record = self.tick()
        self.assertTrue(record['sent'], record['decision'])

    def test_success_notification_respects_activity_deadline_and_pause(self):
        self.add_result('qualified-1')
        campaign = json.loads((self.root/'campaign.json').read_text())
        cases = [('parent_active', 'running', None, campaign),
                 ('child_active', 'idle', {'kind': 'writing'}, campaign),
                 ('deadline', 'idle', None, {**campaign, 'deadline_utc': '2020-01-01T00:00:00+00:00'}),
                 ('paused', 'idle', None, {**campaign, 'status': 'paused'})]
        for name, parent_status, activity, current_campaign in cases:
            with self.subTest(name=name):
                self.status['status'] = parent_status
                self.status['children']['child']['activity'] = activity
                self.write(self.root/'campaign.json', current_campaign)
                self.assertFalse(self.tick()['sent'])
        self.assertEqual((self.driver/'commands.jsonl').read_text(), '')

    def test_reference_completions_wake_once_without_changing_primary_acceptance(self):
        self.add_result('qualified-primary')
        self.assertTrue(self.tick()['sent'])
        for profile in ('reference-one', 'reference-two'):
            previous = json.loads((self.driver/'commands.jsonl').read_text().splitlines()[-1])
            with (self.driver/'events.jsonl').open('a') as stream:
                stream.write(json.dumps({'type': 'response', 'command': 'prompt',
                                         'id': previous['id'], 'success': True})+'\n')
            os.utime(self.driver/'status.json', None)
            self.add_result(profile, eligible=False)
            path = self.workspace/'results'/profile/'raw.json'
            raw = json.loads(path.read_text())
            raw.update(resource_profile=profile, reason_codes=['reference_profile_not_promotable'])
            self.write(path, raw)
            saved = json.loads((self.root/'observer/state.json').read_text())
            saved['idle_since'] = self.now-91
            self.write(self.root/'observer/state.json', saved)
            record = self.tick()
            self.assertTrue(record['sent'])
            self.assertEqual(record['decision']['result_id'], profile)
            self.assertEqual(record['decision']['public_acceptance'],
                             {'action': 'qualified', 'result_id': 'qualified-primary'})
            count = len((self.driver/'commands.jsonl').read_text().splitlines())
            self.assertFalse(self.tick()['sent'])
            self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), count)

    def test_reference_result_cannot_qualify_as_primary(self):
        self.add_result('reference-only')
        path = self.workspace/'results/reference-only/raw.json'
        raw = json.loads(path.read_text())
        raw.update(resource_profile='reference-two')
        self.write(path, raw)
        ledger = json.loads((self.workspace/'state/state.json').read_text())
        decision = observer.public_decision(ledger, observer.public_rows(self.workspace, ledger))
        self.assertEqual(decision['action'], 'retry')
        self.assertIsNone(decision['result_id'])

    def test_owner_result_does_not_replace_a_public_completion_event(self):
        self.add_result('qualified-primary')
        self.assertTrue(self.tick()['sent'])
        previous = json.loads((self.driver/'commands.jsonl').read_text())
        (self.driver/'events.jsonl').write_text(json.dumps({'type': 'response', 'command': 'prompt',
            'id': previous['id'], 'success': True})+'\n')
        os.utime(self.driver/'status.json', None)
        self.add_result('owner-result')
        path = self.workspace/'results/owner-result/raw.json'
        raw = json.loads(path.read_text())
        raw.update(track='owner', resource_profile='reference-two')
        self.write(path, raw)
        record = self.tick()
        self.assertFalse(record['sent'])
        self.assertEqual(record['decision']['action'], 'event_already_acknowledged')
        self.assertEqual(record['decision']['basis']['result_id'], 'qualified-primary')

    def test_last_allowed_full_result_is_delivered_before_budget_stop(self):
        self.add_result('last-trial', eligible=True)
        path = self.workspace/'state/state.json'
        ledger = json.loads(path.read_text())
        ledger['budgets']['agent']['evaluations'] = ledger['search_budget']['candidate_evaluations']
        self.write(path, ledger)
        record = self.tick()
        self.assertTrue(record['sent'])
        self.assertEqual(record['decision']['result_id'], 'last-trial')
        command = json.loads((self.driver/'commands.jsonl').read_text())
        self.assertIn('exhausted', command['message'])
        self.assertIn('report', command['message'])

    def test_explicitly_open_ended_authorization_keeps_feedback_and_budget_guards(self):
        campaign = json.loads((self.root/'campaign.json').read_text())
        campaign['deadline_utc'] = None
        self.write(self.root/'campaign.json', campaign)
        self.add_result('qualified-1')
        self.assertTrue(self.tick()['sent'])
        command = json.loads((self.driver/'commands.jsonl').read_text())
        self.assertNotIn('2026-09-08 01:00 UTC', command['message'])
        ledger_path = self.workspace/'state/state.json'
        ledger = json.loads(ledger_path.read_text())
        ledger['budgets']['agent']['evaluations'] = ledger['search_budget']['candidate_evaluations']
        self.write(ledger_path, ledger)
        exhausted = self.tick()
        self.assertFalse(exhausted['sent'])
        self.assertEqual(exhausted['decision']['action'], 'awaiting_native_ack')
        self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), 1)

    def test_success_notification_recovers_queue_append_without_resending(self):
        self.add_result('qualified-1')
        self.assertTrue(self.tick()['sent'])
        saved = json.loads((self.root/'observer/state.json').read_text())
        saved['deliveries'] = {}
        saved['idle_since'] = self.now-91
        self.write(self.root/'observer/state.json', saved)
        os.utime(self.driver/'status.json', None)
        recovered = self.tick()
        self.assertFalse(recovered['sent'])
        self.assertEqual(recovered['delivery']['status'], 'queued')
        self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), 1)

    def test_busy_lock_keeps_one_pending_receipt_without_dispatch(self):
        first = self.tick('busy')
        self.assertEqual(first['decision']['action'], 'benchmark_busy')
        self.assertFalse(first['sent'])
        self.assertEqual((self.driver/'commands.jsonl').read_text(), '')
        saved = json.loads((self.root/'observer/state.json').read_text())
        self.assertEqual(len(saved['deliveries']), 1)
        receipt = next(iter(saved['deliveries'].values()))
        self.assertEqual(receipt['status'], 'pending')
        self.assertEqual(self.tick('busy')['delivery']['id'], receipt['id'])
        self.assertTrue(self.tick('free')['sent'])
        self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), 1)

    def test_dispatch_needs_native_ack_and_never_resends(self):
        self.assertTrue(self.tick()['sent'])
        command = json.loads((self.driver/'commands.jsonl').read_text())
        os.utime(self.driver/'status.json', None)
        record = self.tick()
        self.assertEqual(record['delivery']['status'], 'queued')
        self.assertFalse(record['sent'])
        self.assertFalse(record['acknowledged'])
        (self.driver/'events.jsonl').write_text(json.dumps({'type': 'response', 'command': 'prompt',
                                                         'id': command['id'], 'success': True})+'\n')
        record = self.tick()
        self.assertEqual(record['delivery']['status'], 'acknowledged')
        self.assertTrue(record['acknowledged'])
        self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), 1)
        self.assertEqual(json.loads((self.root/'observer/state.json').read_text())['delivered'], ['old-event'])

    def test_new_unconfirmed_parent_work_suppresses_wake(self):
        self.status['parent_idle_receipt'] = None
        record = self.tick()
        self.assertEqual(record['decision']['action'], 'native_idle_unconfirmed')
        self.assertFalse(record['sent'])

    def test_crash_after_queue_append_is_recovered_without_duplicate(self):
        self.assertTrue(self.tick()['sent'])
        saved = json.loads((self.root/'observer/state.json').read_text())
        saved['deliveries'] = {}
        saved['idle_since'] = self.now-91
        self.write(self.root/'observer/state.json', saved)
        os.utime(self.driver/'status.json', None)
        record = self.tick()
        self.assertEqual(record['delivery']['status'], 'queued')
        self.assertFalse(record['sent'])
        self.assertEqual(len((self.driver/'commands.jsonl').read_text().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
