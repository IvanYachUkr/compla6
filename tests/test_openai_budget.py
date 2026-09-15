import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from openai_budget import Budget, Stopped, CAP_NANO


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.json'
        self.b = Budget(self.path)

    def reserve(self, model='gpt-5.6-luna', n=1000, out=1000):
        return self.b.reserve('run1', 'session1', model, n, out, 'digest')

    def settle(self, ticket, model='gpt-5.6-luna', **usage):
        self.b.settle(ticket, usage, response_id='resp_test', response_model=model, service_tier='default')

    def test_missing_ledger_and_recreation_fail(self):
        with self.assertRaises(Stopped): self.b.snapshot()
        self.b.create()
        with self.assertRaises(Stopped): self.b.create()
        with self.assertRaises(Stopped): Budget(self.path).create(CAP_NANO + 1)

    def test_parallel_requests_share_one_atomic_cap(self):
        self.b.create(2_900_000)
        def one(_):
            try: return self.reserve()
            except Stopped: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(one, range(20)))
        self.assertEqual(sum(t is not None for t in tickets), 2)
        self.assertEqual(self.b.snapshot()['remaining_nano'], 0)

    def test_usage_refunds_only_verified_unused_reservation(self):
        self.b.create()
        t = self.reserve()
        self.settle(t, input_tokens=100, output_tokens=200,
                    input_tokens_details={'cached_tokens':20, 'cache_write_tokens':30})
        self.assertEqual(self.b.snapshot()['committed_and_reserved_nano'], 257900)
        with self.assertRaises(Stopped): self.settle(t, input_tokens=0, output_tokens=0)

    def test_unknown_cache_writes_use_conservative_rate(self):
        self.b.create()
        t = self.reserve()
        self.settle(t, input_tokens=100, output_tokens=0)
        self.assertEqual(self.b.snapshot()['committed_and_reserved_nano'], 25000)

    def test_uncertain_request_stops_every_run_and_keeps_reservation(self):
        self.b.create()
        t = self.reserve()
        before = self.b.snapshot()['committed_and_reserved_nano']
        self.b.uncertain(t, 'connection_lost')
        self.assertEqual(Budget(self.path).snapshot()['committed_and_reserved_nano'], before)
        with self.assertRaises(Stopped): self.reserve('gpt-5.6-sol')

    def test_long_context_output_and_input_rates_are_both_reserved(self):
        self.b.create()
        self.reserve('gpt-5.6-sol', 272001, 128000)
        self.assertEqual(self.b.snapshot()['committed_and_reserved_nano'], 272001 * 10000 + 128000 * 30000)

    def test_bad_usage_or_wrong_model_never_refunds(self):
        self.b.create()
        t = self.reserve()
        before = self.b.snapshot()['committed_and_reserved_nano']
        with self.assertRaises(Stopped):
            self.settle(t, model='gpt-5.6-sol', input_tokens=100, output_tokens=0)
        self.assertEqual(self.b.snapshot()['committed_and_reserved_nano'], before)
        self.assertIsNotNone(self.b.snapshot()['stopped'])

    def test_reviewed_native_stop_retains_all_charges_and_records_recovery(self):
        self.b.create()
        ticket = self.reserve()
        self.settle(ticket, input_tokens=100, output_tokens=200)
        reason = 'native_native_state_error'
        self.b.stop(reason)
        before = self.b.snapshot()
        self.b.recover_infrastructure(reason, 'owner-review-1')
        after = self.b.snapshot()
        self.assertIsNone(after['stopped'])
        for key in ('cap_nano', 'requests', 'committed_and_reserved_nano', 'remaining_nano'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['recoveries'][0]['stop_reason'], reason)
        with self.assertRaises(Stopped): self.b.recover_infrastructure(reason, 'owner-review-1')

    def test_recovery_cannot_clear_pending_uncertain_or_budget_stops(self):
        self.b.create()
        ticket = self.reserve()
        reason = 'native_native_state_error'
        self.b.stop(reason)
        with self.assertRaisesRegex(Stopped, 'unresolved_request'):
            self.b.recover_infrastructure(reason, 'pending')
        self.b.uncertain(ticket, 'connection_lost')
        with self.assertRaisesRegex(Stopped, 'unresolved_request'):
            self.b.recover_infrastructure(reason, 'uncertain')
        for stop in ('budget_cannot_cover_next_request', 'usage_or_model_verification_failed'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as root:
                budget = Budget(Path(root)/'budget.json'); budget.create(); budget.stop(stop)
                with self.assertRaisesRegex(Stopped, 'nonrecoverable_stop'):
                    budget.recover_infrastructure(stop, 'forbidden')


if __name__ == '__main__': unittest.main()
