"""Regressions for contradictory run instructions and unusable launch interfaces."""
import copy
import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import candidate, dataset, research
from compression_lab.engine import Engine, ResearcherEngine
from compression_lab.util import Error, load, save
from test_engine import data


class RunInstructionTests(unittest.TestCase):
    def test_card_creation_snapshots_the_whole_input_and_exposes_current_prompt(self):
        from compression_lab.cli import dispatch, parser
        self.assertIn('card-create', parser().format_help())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'input.txt'; source.write_bytes(b'complete data\x00\xff')
            made = dispatch(parser().parse_args(['card-create','--dataset-id','fresh',
                '--input',str(source),'--output',str(root/'data')]))
            card_path = Path(made['metrics']['card_path'])
            card, rows = dataset.read_source(card_path)
            self.assertEqual(card['evaluation_mode'], 'whole_dataset')
            self.assertEqual(rows[0]['source'].read_bytes(), source.read_bytes())
            source.write_bytes(b'changed later')
            self.assertEqual(rows[0]['source'].read_bytes(), b'complete data\x00\xff')
            engine = Engine.init(root/'workspace', card_path, exploratory=True)
            from compression_lab.instructions import prompt
            text = prompt(engine)
            self.assertIn('from_scratch', text)
            self.assertNotIn('Mature libraries are encouraged', text)
            self.assertIn('export_and_report', text)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'Official MCP SDK unavailable')
    def test_mcp_advertises_only_configured_operations(self):
        from mcp.server.fastmcp import FastMCP
        from compression_lab.mcp_server import serve
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); card_path = data(root/'data')
            save(card_path, {**load(card_path), 'implementation_policy':'from_scratch', 'baseline_visibility':'hidden'})
            engine = Engine.init(root/'workspace', card_path, exploratory=True)
            with patch.object(FastMCP, 'run', autospec=True) as run:
                serve(engine.root)
            server = run.call_args.args[0]
            tools = {tool.name:tool for tool in asyncio.run(server.list_tools())}
            self.assertFalse({'baselines','baseline_trial','recipe_create','finish','experiment_brief','inventory'} & tools.keys())
            self.assertTrue({'manifest_template','record_hypothesis','feedback','compare'} <= tools.keys())
            self.assertEqual(tools['feedback'].inputSchema['required'], ['result_id'])
            self.assertEqual(tools['compare'].inputSchema['required'], ['result_ids'])

    def test_new_card_defaults_do_not_reinterpret_legacy_cards(self):
        self.assertTrue(hasattr(dataset, 'research_card'), 'New research needs whole-dataset defaults')
        card = dataset.research_card('new-run')
        self.assertEqual(dataset.validate(card), card)
        self.assertEqual(dataset.public_partitions(card), ('corpus',))
        self.assertEqual(card['timing_policy']['operation'], 'offline-plus-online-v1')
        self.assertEqual(card['timing_policy']['encoding_floor_scope'], 'combined')
        self.assertEqual(card['implementation_policy'], 'from_scratch')
        self.assertEqual(card['primary_size_policy'], 'standard-codec-available-v1')
        self.assertEqual(dataset.public_partitions(dataset.example_card('legacy')), ('train', 'development'))
        if importlib.util.find_spec('jsonschema'):
            import jsonschema
            schema = load(Path(__file__).resolve().parents[1]/'schemas/dataset-card.schema.json')
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate({**card,'baseline_visibility':'hidden','implementation_policy':'from_scratch'}, schema)
            jsonschema.validate(dataset.example_card('legacy'), schema)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({**card,'runtime_scenarios':{'schema_version':1,'profiles':[]}}, schema)
            legacy = dataset.example_card('legacy-floor')
            legacy['timing_policy']['encoding_floor_scope'] = 'online'
            with self.assertRaises(Error): dataset.validate(legacy)
            with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(legacy, schema)

    def test_all_mode_combinations_resolve_without_conflicting_instructions(self):
        from compression_lab.instructions import contract
        for mode in ('split', 'whole_dataset'):
            for visibility in ('visible', 'hidden'):
                for implementation in ('open', 'from_scratch'):
                    with self.subTest(mode=mode, visibility=visibility, implementation=implementation):
                        card = dataset.example_card('matrix')
                        card.update(evaluation_mode=mode, baseline_visibility=visibility, implementation_policy=implementation)
                        class Run:
                            def metadata(self): return card, []
                            def state(self): return {'card_digest':'fixture'}
                            def _controller(self):
                                class Controller:
                                    def state(self): return {'protocol':{'schema_version':1}, 'protocol_digest':'fixture-protocol'}
                                return Controller()
                        text = '\n'.join(contract(Run()))
                        self.assertEqual('complete supplied corpus' in text, mode == 'whole_dataset')
                        self.assertEqual('Established libraries' in text, implementation == 'open')
                        self.assertEqual('comparisons are hidden' in text, visibility == 'hidden')
                        self.assertIn('controller_finish', text)
                        self.assertNotIn('export_and_report', text)

    def test_completion_opt_in_is_hidden_by_default_and_bound_to_configuration(self):
        from compression_lab.instructions import configuration, prompt
        from compression_lab.util import digest
        class Controller:
            protocol = {'schema_version':1}
            def state(self): return {'protocol':self.protocol, 'protocol_digest':digest(self.protocol)}
        controller = Controller()
        class Run:
            def metadata(self): return dataset.research_card('completion'), []
            def state(self): return {'card_digest':'fixture'}
            def _controller(self): return controller
        run = Run(); original = configuration(run)
        self.assertEqual(original['controller_protocol_digest'], digest({'schema_version':1}))
        self.assertNotIn('stop_on_success', prompt(run))
        controller.protocol = {'schema_version':1, 'objective':'qualification', 'stop_on_success':False}
        self.assertNotIn('stop_on_success', prompt(run))
        controller.protocol['stop_on_success'] = True
        enabled = configuration(run)
        self.assertIs(enabled['stop_on_success'], True)
        self.assertNotEqual(original['configuration_digest'], enabled['configuration_digest'])

    def test_timing_guidance_resolves_from_the_sealed_policy(self):
        from compression_lab.instructions import contract
        class Run:
            card = dataset.example_card('legacy')
            def metadata(self): return self.card, []
            def state(self): return {'card_digest':'fixture'}
            def _controller(self): return None
        run = Run()
        self.assertNotIn('online encoding only', '\n'.join(contract(run)))
        run.card = dataset.research_card('staged')
        current = '\n'.join(contract(run))
        self.assertIn('compression software', current)
        self.assertIn('bare minimum', current)
        self.assertIn('fast mode', current)
        self.assertIn('maximum-compression mode', current)
        self.assertIn('end-to-end', current)
        self.assertNotIn('online encoding only', current)
        run.card['timing_policy']['encoding_floor_scope'] = 'online'
        self.assertIn('online encoding only', '\n'.join(contract(run)))

    def test_inactive_speed_and_hypothesis_requirements_are_omitted(self):
        from compression_lab.instructions import contract
        class Run:
            card = dataset.research_card('package-size')
            def metadata(self): return self.card, []
            def state(self): return {'card_digest':'fixture'}
            def _controller(self):
                class Controller:
                    def state(self): return {'protocol':{'schema_version':1}, 'protocol_digest':'fixture'}
                return Controller()
        run = Run()
        run.card['objective']['encode_floor_bytes_per_second'] = None
        run.card['hypothesis_policy'] = 'optional'
        for scope in ('combined', 'online'):
            with self.subTest(scope=scope):
                run.card['timing_policy']['encoding_floor_scope'] = scope
                text = '\n'.join(contract(run))
                self.assertIn('package size', text)
                self.assertNotIn('floor', text)
                self.assertNotIn('fast mode', text)
                self.assertNotIn('qualified fast', text)
                self.assertNotIn('record_hypothesis', text)
                self.assertIn('status(job_id=job_id)', text)

    def test_native_prompt_resolves_variants_framing_and_objectives(self):
        from compression_lab.instructions import native_prompt
        config = dict(row_framing='nul', trials=7, warmups=1, memory_bytes=2*1024**3,
                      selectivities=[1,3,10,30,100], compression_speed='competitive with matched baselines')
        commission = dict(dataset='queries', original_bytes=42, implementation='open',
                          variants=['bulk','rows'], objectives=['package_bytes','decode_seconds'],
                          workbench='/work/workbench', public_inputs=[{'name':'queries'}])
        text = native_prompt(config, commission)
        self.assertIn('NUL', text)
        self.assertIn('lab_rows', text)
        self.assertIn('setup plus reconstruction', text)
        self.assertIn('Existing codec libraries', text)
        self.assertIn('lab_encode', text)
        self.assertIn('status(job_id=job_id)', text)
        self.assertNotIn('floor', text)
        self.assertNotIn('fast mode', text)
        config['row_framing'] = 'none'
        commission.update(implementation='from_scratch', variants=['bulk'], objectives=['package_bytes'])
        text = native_prompt(config, commission)
        self.assertIn('from scratch', text)
        self.assertNotIn('lab_rows', text)
        self.assertNotIn('selected-row', text)
        self.assertNotIn('Existing codec libraries', text)
        self.assertNotIn('Reduce complete package size and', text)

    def test_prepared_workbench_uses_generated_prompt_and_preserves_existing_files(self):
        import hashlib
        import importlib.util
        spec = importlib.util.spec_from_file_location('prepare_agent', Path(__file__).resolve().parents[1]/'tools/prepare_agent.py')
        helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); engine = Engine.init(root/'workspace', data(root/'data'), exploratory=True)
            entry = helper.prepare(engine.root)
            receipt = load(engine.root/'workbench/instruction-receipt.json')
            from compression_lab.instructions import prompt
            self.assertTrue(entry.read_text().startswith(prompt(ResearcherEngine(engine.root))))
            self.assertIn(str(entry.parent/'lab.py'), entry.read_text())
            self.assertTrue((entry.parent/'agent').is_dir())
            for name, expected in receipt['files'].items():
                self.assertEqual(hashlib.sha256((entry.parent/name).read_bytes()).hexdigest(), expected)
            entry.write_text('retain this user change')
            with self.assertRaises(Error) as caught:
                helper.prepare(engine.root)
            self.assertEqual(caught.exception.code, 'workbench_file_exists')
            self.assertEqual(entry.read_text(), 'retain this user change')

    def test_brief_resolves_whole_hidden_from_scratch_without_library_advice(self):
        class Stub:
            def metadata(self):
                card = dataset.example_card('instructions')
                card.update(evaluation_mode='whole_dataset', implementation_policy='from_scratch', baseline_visibility='hidden')
                return card, []
            def state(self):
                return dict(results=[], card_digest='card', runtime_digest='runtime', search_budget={}, budgets={}, active_job=None)
            def baseline_table(self):
                return {'metrics': {'supplied_comparison_available': False}}
            def _controller(self): return None
        caps = {'families': {}, 'recipes': {}}
        with patch('compression_lab.research.inventory.inspect', return_value=caps):
            brief = research.brief(Stub())
        self.assertIn('run_configuration', brief)
        self.assertEqual(brief['run_configuration']['implementation_policy'], 'from_scratch')
        self.assertEqual(brief['run_configuration']['completion_method'], 'export_and_report')
        text = '\n'.join(brief['search_contract'])
        self.assertNotIn('Mature libraries are encouraged', text)
        self.assertNotIn('all allowed', text)
        self.assertEqual(brief['recipe_ids'], [])
        self.assertNotIn('qualified_controls', brief)

    def test_codec_free_manifest_is_complete_without_reading_a_baseline_source(self):
        self.assertTrue(hasattr(candidate, 'blank_manifest'), 'Need a complete codec-free v2 manifest')
        with patch('compression_lab.baselines.create', side_effect=AssertionError('baseline source must not be generated')):
            manifest = candidate.blank_manifest('my-codec')
        self.assertEqual(candidate.manifest(manifest), manifest)
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(manifest['dependencies'], [])
        self.assertEqual(manifest['source_paths'], ['codec.cpp', 'decoder.cpp'])

    def test_from_scratch_recipe_guard_runs_before_factory_or_budget_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); card_path = data(root/'data')
            save(card_path, {**load(card_path), 'implementation_policy': 'from_scratch'})
            try: engine = ResearcherEngine.init(root/'workspace', card_path, exploratory=True)
            except Error as exc:
                self.fail('From-scratch must be a supported sealed card choice: '+str(exc))
            before = copy.deepcopy(engine.state())
            with patch('compression_lab.engine.baselines.create', side_effect=AssertionError('factory must not run')):
                with self.assertRaisesRegex(Error, 'from_scratch_recipe_forbidden'):
                    engine.create_recipe('stored', 'starter')
            self.assertEqual(engine.state(), before)

    def test_hypothesis_is_bound_to_the_run_and_cannot_be_edited(self):
        self.assertTrue(hasattr(Engine, 'record_hypothesis'), 'Need a timestamped pre-experiment receipt')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); engine = Engine.init(root/'workspace', data(root/'data'), exploratory=True)
            receipt = engine.record_hypothesis('Count repeated tokens', 'Fewer archive bytes', 'No size reduction')['metrics']
            from compression_lab.instructions import verify_hypothesis
            manifest = {'hypothesis': {'experiment_id': receipt['experiment_id']}}
            self.assertEqual(verify_hypothesis(engine, manifest), receipt)
            path = engine.root/'hypotheses'/(receipt['experiment_id']+'.json')
            changed = {**receipt, 'expected_benefit': 'rewritten after measurement'}
            save(path, changed)
            with self.assertRaisesRegex(Error, 'stale_hypothesis'):
                verify_hypothesis(engine, manifest)


if __name__ == '__main__': unittest.main()
