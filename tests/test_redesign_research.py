import copy
import importlib
import tempfile
import unittest
from pathlib import Path
from compression_lab import baselines
from compression_lab.engine import Engine
from compression_lab.util import Error, load, save
from test_engine import data


class ResearchTests(unittest.TestCase):
    def test_screen_roundtrip_is_budgeted_not_promotable_and_never_reports_development(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = Engine.init(root/'work', data(root/'data'))
            source = baselines.create(root/'candidate', 'stored', 0)
            registered = engine.register(source)
            result = engine.evaluate(registered['candidate_digest'], 'screen', wait=True)
            self.assertEqual(result['status'], 'ineligible', result)
            self.assertTrue(result['metrics']['screen_passed'], result)
            self.assertFalse(result['metrics']['quality_passed'])
            self.assertFalse(result['metrics']['eligible'])
            self.assertIn('screen_not_promotable', result['reason_codes'])
            raw = engine.raw(result['metrics']['result_id'])
            self.assertNotIn('accounting', raw)
            self.assertNotIn('timing', raw)
            self.assertEqual(raw['screening']['development_bytes_used'], 0)
            self.assertEqual(raw['screening']['objects'][0]['alias'], 'object-0')
            self.assertEqual(len(raw['timing_trials']['encode']), 3)
            self.assertEqual(engine.state()['budgets']['agent']['evaluations'], 1)
            self.assertNotIn('best_eligible_candidate', engine.state())

    def test_inventory_brief_and_feedback_are_read_only(self):
        self.assertIsNotNone(importlib.util.find_spec('compression_lab.research'))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); engine = Engine.init(root/'work', data(root/'data'))
            before = copy.deepcopy(engine.state())
            inventory = engine.inventory()['metrics']
            self.assertEqual(set(inventory['families']), {'stored', 'zstd', 'lz4', 'brotli', 'xz', 'zlib', 'bzip2'})
            self.assertTrue(inventory['families']['zstd']['installed_version'])
            brief = engine.brief()['metrics']
            self.assertTrue(brief['unmeasured_families'])
            self.assertEqual(brief['promotion_policy']['encoding_floor_bytes_per_second'], 100000000)
            self.assertEqual(engine.state(), before)

    def test_baseline_factory_does_not_trust_mutated_named_workbench_or_name_only_cache(self):
        self.assertTrue(hasattr(Engine, 'baseline_trial'), 'content-addressed baseline factory required')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); engine=Engine.init(root/'work', data(root/'data'))
            # Name collides with the conventional recipe, but the source is not that recipe.
            source=baselines.create(engine.root/'workbench'/'stored', 'stored', 0)
            with (source/'codec.cpp').open('a') as f:f.write('\n// not the factory recipe\n')
            old=engine.register(source, 'baseline')
            engine.evaluate(old['candidate_digest'], 'screen', 'baseline', True)
            first=engine.baseline_trial('stored', 'screen', wait=True)
            self.assertNotEqual(first['candidate_digest'], old['candidate_digest'])
            count=engine.state()['budgets']['baseline']['evaluations']
            second=engine.baseline_trial('stored', 'screen', wait=True)
            self.assertEqual(second['metrics']['result_id'], first['metrics']['result_id'])
            self.assertEqual(engine.state()['budgets']['baseline']['evaluations'], count)
            table=engine.baseline_table()['metrics']
            self.assertIsNone(table['best_size'])
            self.assertIsNone(table['best_eligible'])
            self.assertEqual(table['pareto_frontier'], [])
            measured=[row for row in table['rows'] if row.get('result_id')]
            missing=[row for row in table['rows'] if not row.get('result_id')]
            self.assertTrue(measured)
            self.assertTrue(all(row['depth']=='screen' for row in measured))
            self.assertTrue(missing)
            self.assertTrue(all(row['state']=='missing' and row['result_ids']==[] for row in missing))
            self.assertTrue(all('size_rank' not in row for row in table['rows']))
            feedback=engine.feedback(first['metrics']['result_id'])['metrics']
            self.assertEqual(feedback['next_action'], 'full_evaluation_required')

    def test_source_factory_paths_and_unavailable_recipe_fail_closed(self):
        self.assertTrue(hasattr(Engine, 'create_recipe'))
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); engine=Engine.init(root/'work', data(root/'data'))
            with self.assertRaises(Error):engine.create_recipe('zstd-1', '../escape')
            with self.assertRaises(Error):engine.create_recipe('made-up', 'x')
            source=engine.create_recipe('zstd-1', 'try-zstd')['metrics']['candidate_path']
            self.assertTrue((Path(source)/'candidate.json').is_file())
            with self.assertRaises(Error):engine.create_recipe('zstd-1', 'try-zstd')
