import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from test_engine import data

@unittest.skipUnless(importlib.util.find_spec('mcp'),'Official MCP SDK unavailable; not a protocol pass')
class ResearchMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_research_tools_factory_job_and_bounded_feedback(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from compression_lab.engine import Engine
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); e=Engine.init(root/'work',data(root/'data'))
            params=StdioServerParameters(command=sys.executable,args=['-m','compression_lab.mcp_server','--workspace',str(e.root)])
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    names={t.name for t in (await session.list_tools()).tools}
                    self.assertTrue({'inventory','brief','recipe_create','baseline_trial','feedback'}<=names)
                    self.assertFalse({'freeze','evaluate_private','disclose'}&names)
                    async def call(name,arguments=None):
                        response=await session.call_tool(name,arguments or {})
                        self.assertFalse(response.isError,response)
                        return getattr(response,'structuredContent',None) or json.loads(response.content[0].text)
                    inventory=await call('inventory')
                    self.assertEqual(inventory['metrics']['upstream_verified_on'],'2026-09-07')
                    brief=await call('brief');self.assertEqual(brief['metrics']['composition']['train']['objects'],1)
                    template=await call('recipe_create',{'recipe_id':'stored','name':'editable-template'})
                    self.assertFalse(template['metrics']['registered'])
                    job=await call('baseline_trial',{'recipe_id':'stored','depth':'screen'})
                    async with asyncio.timeout(40):
                        while job['status'] in ('queued','running'):
                            await asyncio.sleep(.1)
                            job=await call('status',{'job_id':job['job_id']})
                    self.assertTrue(job['metrics']['screen_passed'],job)
                    self.assertFalse(job['metrics']['eligible'])
                    feedback=await call('feedback',{'result_id':job['metrics']['result_id']})
                    self.assertEqual(feedback['metrics']['next_action'],'full_evaluation_required')
                    self.assertLess(len(json.dumps(feedback)),8000)
                    table=await call('baselines');self.assertEqual(table['metrics']['pareto_frontier'],[])
