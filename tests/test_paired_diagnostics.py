import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from compression_lab import accounting
from compression_lab.util import Error


def result(archives, extra_fixed=0):
    objects = [
        {'alias': name, 'group': group, 'split': split, 'canonical_bytes': size,
         'archive_bytes': archive}
        for name, group, split, size, archive in [
            ('training', 'train', 'train', 9000, archives[0]),
            ('large', 'large-group', 'development', 900, archives[1]),
            ('small', 'small-group', 'development', 100, archives[2])]]
    fixed = dict(fixed_bytes=20 + extra_fixed, config_bytes=10, binary_bytes=30,
                 nonplatform_dependency_bytes=40, packed_source_bytes=50)
    return {'quality_passed': True, 'depth': 'full', 'workload': 'independent_objects',
            'objects': objects, 'fixed_costs': fixed,
            'accounting': {'development': accounting.costs(1000, sum(archives[1:]) + 9, fixed, 10000)},
            'timing': {'encode': {'median_bytes_per_second': 110_000_000, 'relative_MAD': .02},
                       'decode': {'median_bytes_per_second': 90_000_000, 'relative_MAD': .03}}}


class PairedDiagnosticsTests(unittest.TestCase):
    def test_staged_comparison_uses_end_to_end_speed_for_headroom(self):
        reference=result([1,450,50]); candidate=result([1,400,50])
        for raw in (reference,candidate):
            raw.update(timing_operation='offline-plus-online-v1',encoding_floor_scope='combined')
            raw['timing']['combined']={'median_bytes_per_second':9_000_000,'relative_MAD':.04}
        report=accounting.paired_diagnostics(reference,candidate)
        self.assertEqual(report['candidate_timing']['encode_floor_headroom_bytes_per_second'],-91_000_000)
        self.assertEqual(report['candidate_timing']['encoding_floor_scope'],'combined')

    def test_development_pair_excludes_training_and_explains_total(self):
        reference = result([1, 450, 50])
        candidate = result([8000, 400, 60], extra_fixed=75)
        before = copy.deepcopy([reference, candidate])
        report = accounting.paired_diagnostics(reference, candidate)
        self.assertEqual(report['deployment_delta_bytes'], -325)
        self.assertEqual(sum(report['deployment_components_delta_bytes'].values()), -325)
        self.assertEqual(report['deployment_components_delta_bytes']['projected_archive_bytes'], -400)
        self.assertEqual(report['development_archive_delta_bytes'], -40)
        self.assertEqual(report['groups'], 2)
        self.assertEqual(report['largest_group_canonical_byte_share'], .9)
        self.assertEqual(report['worst_object_regressions'], [{'alias': 'small', 'delta_bytes': 10}])
        self.assertEqual(report['candidate_timing']['encode_floor_headroom_bytes_per_second'], 10_000_000)
        self.assertEqual(report['reference_timing']['encode_relative_MAD'], .02)
        self.assertIn('train_plus_development', report['timing_scope'])
        self.assertEqual([reference, candidate], before)
        self.assertEqual(report, accounting.paired_diagnostics(reference, candidate))

    def test_refuses_unsupported_or_unpaired_evidence(self):
        reference = result([1, 450, 50])
        mutations = [
            lambda r: r.update(depth='quick'),
            lambda r: r.update(quality_passed=False),
            lambda r: r.update(workload='mutable_store'),
            lambda r: r.update(objects=[r['objects'][0]]),
            lambda r: r['objects'][1].update(canonical_bytes=901),
            lambda r: r['objects'][1].update(group='other'),
            lambda r: r['objects'].append(copy.deepcopy(r['objects'][1])),
            lambda r: r['accounting']['development'].update(deployment_horizon_bytes=999),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                candidate = copy.deepcopy(reference)
                mutate(candidate)
                with self.assertRaises(Error):
                    accounting.paired_diagnostics(reference, candidate)


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Official MCP SDK unavailable')
class PairedDiagnosticIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_cli_and_mcp_read_same_immutable_pair_without_budget_use(self):
        from test_engine import data
        from compression_lab import baselines
        from compression_lab.cli import parser, dispatch
        from compression_lab.engine import Engine
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = Engine.init(root / 'workspace', data(root / 'data'))
            baselines.create(root / 'candidate', 'zstd', 1)
            cid = engine.register(root / 'candidate')['candidate_digest']
            ids = [engine.evaluate(cid, 'full', wait=True)['metrics']['result_id'] for _ in range(2)]
            before = engine.state()
            plain = engine.compare(ids)
            self.assertNotIn('paired_diagnostics', plain['metrics'])
            expected = engine.compare(ids, diagnostics=True)
            self.assertEqual(expected['metrics']['paired_diagnostics']['deployment_delta_bytes'], 0)
            args = parser().parse_args(['compare', '--workspace', str(engine.root), '--results', *ids, '--diagnostics'])
            self.assertEqual(dispatch(args), expected)
            params = StdioServerParameters(command=sys.executable, args=['-m', 'compression_lab.mcp_server', '--workspace', str(engine.root)])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.call_tool('compare', {'result_ids': ids, 'diagnostics': True})
                    self.assertFalse(response.isError, response)
                    value = getattr(response, 'structuredContent', None) or json.loads(response.content[0].text)
                    self.assertEqual(value, expected)
                    tools = Path(__file__).resolve().parents[1]/'tools'
                    with patch.object(sys, 'path', [str(tools), *sys.path]):
                        spec = importlib.util.spec_from_file_location('lab_http_client', tools/'lab.py')
                        helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
                    feedback = await helper.dispatch(session, 'feedback', {'result_id':ids[0]}, engine.root)
                    self.assertEqual(feedback, engine.feedback(ids[0]))
                    compared = await helper.dispatch(session, 'compare', {'result_ids':ids,'diagnostics':True}, engine.root)
                    self.assertEqual(compared, expected)
            self.assertEqual(engine.state(), before)


if __name__ == '__main__':
    unittest.main()
