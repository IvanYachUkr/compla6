import copy
import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from compression_lab import dataset, research, strings
from compression_lab.policy import PublicPolicy
from compression_lab.util import Error, digest, save, sha
from test_controller import EvidenceEngine


class StringsWorkloadTests(unittest.TestCase):
    def test_binary_rows_are_lossless_and_only_lf_is_a_boundary(self):
        for raw,expected in [(b'',[]),(b'\n',[b'\n']),
            (b'one\r\ntwo\x00\xff\rlast',[b'one\r\n',b'two\x00\xff\rlast']),
            (b'a\n\n',[b'a\n',b'\n'])]:
            self.assertEqual(strings.rows(raw),expected)
            self.assertEqual(b''.join(strings.rows(raw)),raw)

    def test_selectivities_are_reproducible_nested_unique_and_complete(self):
        for count in (0,1,7,1001):
            sets=strings.selections(count,17)
            self.assertEqual(sets,strings.selections(count,17))
            previous=set()
            for ids in sets.values():
                self.assertEqual(ids,sorted(set(ids)))
                self.assertTrue(previous<=set(ids));previous=set(ids)
            self.assertEqual(sets['100'],list(range(count)))

    def test_explicit_null_disables_floor_without_changing_defaults(self):
        card=dataset.research_card('strings')
        self.assertEqual(card['objective']['encode_floor_bytes_per_second'],100_000_000)
        card['objective']['encode_floor_bytes_per_second']=None
        dataset.validate(card)
        del card['objective']['encode_floor_bytes_per_second']
        with self.assertRaises(Error):dataset.validate(card)

    def test_slow_correct_result_can_qualify_but_missing_timing_cannot(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=EvidenceEngine(tmp);engine.card['objective']['encode_floor_bytes_per_second']=None
            engine._controller=lambda:None
            with patch('compression_lab.policy.bridge.verify_candidate',return_value=({},{})):
                rid=engine.add(timing={'encode':{'median_bytes_per_second':1}})
                self.assertTrue(PublicPolicy(engine).assess(rid)['candidate_qualified'])
                feedback=research.feedback(engine,rid)
                self.assertIsNone(feedback['encode_floor_headroom_MBps'])
                rid=engine.add('missing',timing={})
                self.assertFalse(PublicPolicy(engine).assess(rid)['candidate_qualified'])

    def test_changed_results_and_unmatched_workloads_cannot_be_compared(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'results').mkdir();evidence=root/'evidence';evidence.mkdir()
            trials=evidence/'trials.jsonl';trials.write_text('fixture\n')
            ids=[]
            for workload in ('first','different'):
                row=dict(quality_passed=True,depth='full',workload_digest=workload,
                         evidence=str(evidence),raw_trials_sha256=sha(trials))
                rid='s-'+digest(row);save(root/'results'/(rid+'.json'),{**row,'result_id':rid});ids.append(rid)
            with self.assertRaises(Error) as caught:strings.compare(root,ids)
            self.assertEqual(caught.exception.code,'strings_unmatched_workloads')
            path=root/'results'/(ids[0]+'.json');path.write_text(path.read_text().replace('first','edited'))
            with self.assertRaises(Error) as caught:strings.compare(root,[ids[0]])
            self.assertEqual(caught.exception.code,'strings_result_changed')

    @unittest.skipUnless(importlib.util.find_spec('mcp'),'Official MCP SDK unavailable')
    def test_mcp_requires_the_matching_commission_and_exposes_async_tools(self):
        from mcp.server.fastmcp import FastMCP
        from compression_lab.mcp_server import serve
        with tempfile.TemporaryDirectory() as temp:
            engine=MagicMock();engine._hidden.return_value=False;engine._controller.return_value=None
            card=dataset.research_card('strings');card.update(implementation_policy='open',baseline_visibility='visible')
            card['objective']['encode_floor_bytes_per_second']=None
            engine.metadata.return_value=(card,[{'canonical_sha256':'abc','canonical_bytes':4}])
            with patch.dict(os.environ,{'COMPRESSION_LAB_STRINGS_WORKSPACE':temp}), \
                 patch('compression_lab.mcp_server.ResearcherEngine',return_value=engine), \
                 patch('compression_lab.strings.config',return_value={'columns':[{'sha256':'abc','bytes':4}]}), \
                 patch.object(FastMCP,'run',autospec=True) as run:
                serve(temp)
                names={t.name for t in asyncio.run(run.call_args.args[0].list_tools())}
                self.assertTrue({'strings_profile','strings_submit','strings_status','strings_compare'}<=names)
                card['objective']['encode_floor_bytes_per_second']=100_000_000
                with self.assertRaises(Error) as caught:serve(temp)
                self.assertEqual(caught.exception.code,'strings_commission_mismatch')


if __name__=='__main__':unittest.main()
