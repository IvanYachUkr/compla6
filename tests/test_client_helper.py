"""Public client presentation/permission regressions from the Sol session."""
import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('lab_client', Path(__file__).resolve().parents[1]/'tools/lab_client.py')
client = importlib.util.module_from_spec(spec); spec.loader.exec_module(client)


class ClientHelperTests(unittest.TestCase):
    def test_sdk_and_bridge_response_shapes(self):
        wanted = {'status': 'eligible', 'metrics': {'quality_passed': True}}
        import json
        class SDK:
            def model_dump(self): return {'structured_content': wanted}
        for value in (wanted, json.dumps(wanted), SDK(), {'content': [{'type': 'text', 'text': json.dumps(wanted)}]}):
            self.assertEqual(client.unpack(value), wanted)
        self.assertEqual(client.unpack({'isError': True, 'content': [{'type':'text','text':'bad candidate'}]})['status'], 'tool_error')

    def test_bounded_display_retains_full_result(self):
        full = {'status':'ok','metrics':{'rows':[{'result_id':str(i)} for i in range(30)],'pareto_frontier':[1]*30,'best_size':{},'best_eligible':{'result_id':'0'}}}
        class Bridge:
            async def call_tool(self, server, tool, args): return full
        lab=client.LabClient(Bridge())
        short=asyncio.run(lab.call('baselines'))
        self.assertIs(lab.last, full)
        self.assertEqual(len(lab.last['metrics']['rows']),30)
        self.assertEqual(short['metrics']['rows']['remaining_items'],18)
        self.assertNotIn('pareto_frontier',short['metrics'])
        self.assertEqual(short['metrics']['best_eligible_result_id'],'0')

    def test_register_repairs_only_public_owned_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'workbench/agent/candidate';p.mkdir(parents=True)
            f=p/'candidate.json';f.write_text('{}');f.chmod(0o600)
            private=root/'private';private.write_text('canary');private.chmod(0o600)
            class Bridge:
                async def call_tool(self, server, tool, args):
                    self_path=Path(args['candidate_path']); assert self_path==p
                    assert (self_path/'candidate.json').stat().st_mode & 0o777 == 0o644
                    return {'status':'registered','candidate_digest':'test'}
            result=asyncio.run(client.LabClient(Bridge(),root).call('register',candidate_path='workbench/agent/candidate'))
            self.assertEqual(result['status'],'registered')
            self.assertEqual(private.stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError):client.prepare_candidate(root,private)

    def test_symlink_ancestor_and_leaf_do_not_change_target_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);base=root/'workbench/agent';base.mkdir(parents=True)
            external=root/'external';external.mkdir();f=external/'private';f.write_text('canary');f.chmod(0o600)
            (base/'linked').symlink_to(external,target_is_directory=True)
            with self.assertRaises(OSError):client.prepare_candidate(root,base/'linked')
            p=base/'candidate';p.mkdir();(p/'linked').symlink_to(f)
            with self.assertRaises(ValueError):client.prepare_candidate(root,p)
            self.assertEqual(f.stat().st_mode & 0o777,0o600)

if __name__=='__main__':unittest.main()
