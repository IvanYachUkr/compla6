"""Terra reservations and settlement use the published Standard token prices."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from openai_budget import Budget, Stopped


class TerraBudgetTests(unittest.TestCase):
    def reserve(self, budget, inputs, outputs):
        try:
            return budget.reserve('terra-yelp', 'root', 'gpt-5.6-terra', inputs, outputs, 'digest')
        except Stopped as error:
            self.fail('The commissioned Terra model cannot be metered: '+str(error))

    def test_short_context_settlement_accounts_for_reads_writes_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = Budget(Path(directory) / 'budget.json')
            budget.create()
            ticket = self.reserve(budget, 1000, 1000)
            self.assertEqual(budget.snapshot()['committed_and_reserved_nano'], 14_500_000)
            budget.settle(ticket, {'input_tokens':100, 'output_tokens':200,
                'input_tokens_details':{'cached_tokens':20, 'cache_write_tokens':30}},
                response_id='fixture', response_model='gpt-5.6-terra', service_tier='default')
            self.assertEqual(budget.snapshot()['committed_and_reserved_nano'], 2_579_000)

    def test_long_context_reservation_covers_premium_and_unknown_cache_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = Budget(Path(directory) / 'budget.json')
            budget.create()
            ticket = self.reserve(budget, 272001, 128000)
            self.assertEqual(budget.snapshot()['committed_and_reserved_nano'], 3_664_005_000)
            budget.settle(ticket, {'input_tokens':272001, 'output_tokens':1000},
                response_id='fixture', response_model='gpt-5.6-terra', service_tier='default')
            self.assertEqual(budget.snapshot()['committed_and_reserved_nano'], 1_378_005_000)


if __name__ == '__main__':
    unittest.main()
