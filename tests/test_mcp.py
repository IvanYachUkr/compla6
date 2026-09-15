"""Real SDK initialize/list/call protocol test. Absence is a skip, never a pass claim."""
import importlib.util,json,tempfile,unittest,sys
from pathlib import Path
from test_engine import data
@unittest.skipUnless(importlib.util.find_spec('mcp'),'Official MCP SDK unavailable in offline build environment')
class MCP(unittest.IsolatedAsyncioTestCase):
 async def test_official_stdio_handshake_list_and_profile(self):
  from mcp import ClientSession,StdioServerParameters
  from mcp.client.stdio import stdio_client
  from compression_lab.engine import Engine
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);e=Engine.init(p/'work',data(p/'data'))
   from compression_lab import baselines
   (p/'outside').mkdir();baselines.create(p/'outside'/'candidate','zstd',1)
   (e.root/'workbench'/'linked').symlink_to(p/'outside',target_is_directory=True)
   params=StdioServerParameters(command=sys.executable,args=['-m','compression_lab.mcp_server','--workspace',str(e.root)])
   async with stdio_client(params) as (read,write):
    async with ClientSession(read,write) as session:
     initialized=await session.initialize();self.assertTrue(initialized.protocolVersion)
     tools=await session.list_tools();names={t.name for t in tools.tools};self.assertIn('profile',names);self.assertNotIn('freeze',names);self.assertNotIn('evaluate_private',names)
     call=await session.call_tool('profile',{});self.assertFalse(call.isError)
     r=getattr(call,'structuredContent',None) or json.loads(call.content[0].text)
     self.assertEqual(r['run_id'],e.state()['run_id']);self.assertEqual(r['metrics']['objects'],2)
     denied=await session.call_tool('register',{'candidate_path':'workbench/linked/candidate'})
     self.assertTrue(denied.isError);self.assertEqual(e.state()['registered'],[])
 async def test_relational_protocol_register_evaluate_status_resume_and_export(self):
  import asyncio,time
  from mcp import ClientSession,StdioServerParameters
  from mcp.client.stdio import stdio_client
  from compression_lab.engine import Engine
  from compression_lab.workloads import registry
  from compression_lab.workloads.fixtures import create_dataset
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);e=Engine.init(p/'work',create_dataset(p/'data','mutable_store'))
   registry.create_reference(e.root/'workbench'/'reference')
   params=StdioServerParameters(command=sys.executable,args=['-m','compression_lab.mcp_server','--workspace',str(e.root)])
   async with stdio_client(params) as (read,write):
    async with ClientSession(read,write) as session:
     await session.initialize()
     async def call(name,args):
      value=await session.call_tool(name,args);self.assertFalse(value.isError,value)
      return getattr(value,'structuredContent',None) or json.loads(value.content[0].text)
     profile=await call('profile',{});self.assertEqual(profile['metrics']['workload'],'mutable_store')
     registered=await call('register',{'candidate_path':'workbench/reference'})
     queued=await call('evaluate',{'candidate_digest':registered['candidate_digest'],'depth':'quick','workload':'mutable_store'})
     deadline=time.monotonic()+90
     while True:
      result=await call('status',{'job_id':queued['job_id']})
      if result['status'] not in ('queued','running'):break
      self.assertLess(time.monotonic(),deadline)
      await asyncio.sleep(.5)
     self.assertTrue(result['metrics']['quality_passed'],result)
     resumed=await call('resume',{});self.assertEqual(resumed['metrics']['next_action']['command'],'evaluate')
     exported=await call('export',{'result_id':result['metrics']['result_id'],'filename':'relational-mcp.zip'})
     self.assertEqual(exported['status'],'exported')
if __name__=='__main__':unittest.main()
