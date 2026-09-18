"""Optional stdio transport using the official MCP Python SDK, never imitation JSON.

Pinned integration target: mcp==1.29.1. Owner operations are intentionally absent.
This module fails clearly when the optional dependency is not installed.
"""
import argparse,hmac,os,sys
from pathlib import Path
from .engine import ResearcherEngine
from .util import Error,reply

def serve(workspace,http_port=None,bearer_token_env=None):
 try:
  from mcp.server.fastmcp import FastMCP
 except ImportError as e:
  print('Official MCP v1 adapter unavailable: install compression-lab[mcp] (mcp==1.29.1). An absent SDK or incompatible v2 install cannot serve this adapter. The core CLI does not need MCP.',file=sys.stderr);raise SystemExit(2) from e
 root=Path(workspace).absolute()
 if (root/'owner-policy.json').exists():raise Error('private_workspace_forbidden')
 engine=ResearcherEngine(root);options={}
 export_root=root/('researcher-exports' if engine._hidden() else 'exports')
 export_root.mkdir(mode=0o755,exist_ok=True)
 if export_root.is_symlink():raise Error('unsafe_export_directory')
 if http_port is not None:
  if not 1<=http_port<=65535:raise Error('invalid_http_port')
  token=os.environ.get(bearer_token_env or '', '')
  if len(token)<32:raise Error('http_bearer_token_required','Set a random bearer token of at least 32 characters in the named environment variable')
  from mcp.server.auth.provider import AccessToken
  from mcp.server.auth.settings import AuthSettings
  class LocalTokenVerifier:
   async def verify_token(self,presented):
    if hmac.compare_digest(presented,token):return AccessToken(token=presented,client_id='local-public-agent',scopes=['public'])
    return None
  url=f'http://127.0.0.1:{http_port}'
  options={'host':'127.0.0.1','port':http_port,'stateless_http':True,'json_response':True,
           'token_verifier':LocalTokenVerifier(),'auth':AuthSettings(issuer_url=url,resource_server_url=url+'/mcp',required_scopes=['public'])}
 server=FastMCP('Compression Lab',**options)

 strings_root=os.environ.get('COMPRESSION_LAB_STRINGS_WORKSPACE')
 if strings_root:
  from . import strings
  strings_config=strings.config(strings_root)
  strings_card,strings_objects=engine.metadata()
  if (strings_card.get('evaluation_mode')!='whole_dataset' or
      strings_card.get('implementation_policy')!='open' or
      strings_card.get('baseline_visibility')!='visible' or
      strings_card['objective']['encode_floor_bytes_per_second'] is not None):
   raise Error('strings_commission_mismatch','Use the commissioned whole-dataset open card with visible baselines and an explicit null encoding floor')
  if sorted((r['canonical_sha256'],r['canonical_bytes']) for r in strings_objects)!=sorted((r['sha256'],r['bytes']) for r in strings_config['columns']):
   raise Error('strings_dataset_mismatch')
  @server.tool()
  def strings_profile()->dict:
   """Fixed RAM workloads, native C ABI and supplied reference result IDs."""
   return reply(metrics=strings.profile(strings_root))
  @server.tool()
  def strings_submit(candidate_path:str,quick:bool=False)->dict:
   """Submit shared-library encoder/decoder to the fixed RAM and row oracle. Returns a job ID."""
   engine._guard_research()
   manifest=(root/candidate_path).resolve()
   if not manifest.is_relative_to(root/'workbench'):raise Error('candidate_outside_workbench')
   return reply(metrics=strings.submit(strings_root,manifest,quick))
  @server.tool()
  def strings_status(job_id:str)->dict:
   """Read asynchronous string-workload completion and its immutable result ID."""
   return reply(metrics=strings.status(strings_root,job_id))
  @server.tool()
  def strings_compare(result_ids:list[str],include_columns:bool=False)->dict:
   """Read matching full measurements; selected-row and bulk targets remain distinct."""
   return reply(metrics=strings.compare(strings_root,result_ids,include_columns))

 @server.tool()
 def profile()->dict:
  """Verified PUBLIC dataset composition. No private metadata."""
  return engine.profile()
 @server.tool()
 def baselines(resource_profile:str|None=None,candidate_result:str|None=None)->dict:
  """Supplied conventional comparisons when the card permits; otherwise a no-comparison notice."""
  return engine.baseline_table(resource_profile,candidate_result)
 @server.tool()
 def inventory()->dict:
  """Native codec APIs, recipes, installed hashes and separately verified upstream versions."""
  return engine.inventory()
 @server.tool()
 def brief()->dict:
  """Bounded research contract, budgets, unmeasured controls and full-only frontier."""
  return engine.brief()
 @server.tool()
 def experiment_brief()->dict:
  """Authoritative public objective, budgets, outstanding work and next operation."""
  return engine.experiment_brief()
 @server.tool()
 def finish(request_id:str,candidate_digest:str|None=None,result_id:str|None=None,outcome:str='success')->dict:
  """Request public completion with exact immutable evidence; retry with the same request ID."""
  return engine.finish(request_id,candidate_digest,result_id,outcome)
 @server.tool()
 def lesson_propose(entry:dict)->dict:
  """Record an untrusted interpretation and measurable claim from public full results."""
  return engine.lesson_propose(entry)
 @server.tool()
 def lesson_check(lesson_id:str,result_ids:list[str])->dict:
  """Check a structured claim against two fresh comparable results; never runs code."""
  return engine.lesson_check(lesson_id,result_ids)
 @server.tool()
 def lesson_list(limit:int=8)->dict:
  """Bounded scoped lessons; excludes refuted/incompatible entries; prose remains untrusted."""
  return engine.lesson_list(limit)
 @server.tool()
 def feedback(result_id:str)->dict:
  """Evidence-linked diagnosis; screen uses declared fitting objects and never qualifies a candidate."""
  return engine.feedback(result_id)
 @server.tool()
 def manifest_template(name:str='my-codec')->dict:
  """Complete C++ v2 manifest with pinned compiler, no algorithm or codec source."""
  from .candidate import blank_manifest
  return reply(engine.state()['run_id'],metrics={'manifest':blank_manifest(name)})
 @server.tool()
 def record_hypothesis(statement:str,expected_benefit:str,falsifier:str)->dict:
  """Record a short experiment before implementation; include its experiment_id in candidate.json."""
  return engine.record_hypothesis(statement,expected_benefit,falsifier)
 @server.tool()
 def recipe_create(recipe_id:str,name:str)->dict:
  """Create a readable native source template; copy into your own workbench directory when evaluator and agent UIDs differ. No register/evaluate."""
  return engine.create_recipe(recipe_id,name)
 @server.tool()
 def baseline_trial(recipe_id:str,depth:str='screen',resource_profile:str|None=None)->dict:
  """Run a supplied control only when visible; hidden runs receive a no-comparison notice. Own recipes remain available."""
  return engine.baseline_trial(recipe_id,depth,resource_profile=resource_profile)
 @server.tool()
 def register(candidate_path:str)->dict:
  """Snapshot and build a native or declared-runtime mutable candidate located inside the public workspace."""
  p=(root/candidate_path).absolute()
  try:p.relative_to(root)
  except ValueError:raise Error('candidate_outside_public_workspace')
  if any(x=='..' for x in p.parts) or p.is_symlink():raise Error('unsafe_candidate_path')
  return engine.register(p)
 @server.tool()
 def evaluate(candidate_digest:str,depth:str='full',workload:str|None=None,resource_profile:str|None=None,request_id:str|None=None)->dict:
  """Start a durable public job and return its ID immediately."""
  return engine.evaluate(candidate_digest,depth,workload=workload,resource_profile=resource_profile,request_id=request_id)
 @server.tool()
 def compare(result_ids:list[str],diagnostics:bool=False)->dict:
  """Compare exact public results. Optional diagnostics need two full static IDs: reference, candidate."""
  return engine.compare(result_ids,diagnostics)
 @server.tool()
 def resume()->dict:
  """Return one advisory next action; never execute or reset terminal latches."""
  return engine.resume()
 @server.tool()
 def status(job_id:str|None=None,result_id:str|None=None)->dict:
  """Compact durable run/job/result state and next allowed operations."""
  return engine.status(job_id,result_id)
 @server.tool()
 def artifact(result_id:str,name:str='raw.json',offset:int=0,limit:int=16384)->dict:
  """Read an allowlisted bounded public evidence slice with its hash."""
  return engine.artifact(result_id,name,offset,limit)
 @server.tool()
 def cancel(job_id:str)->dict:
  """Cancel a public worker process tree; retain the negative attempt."""
  return engine.cancel(job_id)
 @server.tool()
 def export(result_id:str,filename:str='candidate-export.zip')->dict:
  """Export the exact public result and codec snapshot into the run's public export directory."""
  if Path(filename).name!=filename or not filename.endswith('.zip'):raise Error('unsafe_export_name')
  return engine.export(result_id,export_root/filename)
 card,_=engine.metadata()
 unavailable=set()
 if not engine._controller():unavailable.update(('finish','experiment_brief'))
 if card.get('baseline_visibility')=='hidden':unavailable.update(('baselines','baseline_trial'))
 if card.get('implementation_policy')=='from_scratch':unavailable.update(('recipe_create','inventory','baseline_trial'))
 if card.get('workload')=='mutable_store':unavailable.update(('manifest_template','record_hypothesis','recipe_create','baseline_trial'))
 for name in unavailable:server.remove_tool(name)
 server.run(transport='streamable-http' if http_port is not None else 'stdio')

def main():
 p=argparse.ArgumentParser();p.add_argument('--workspace',required=True);p.add_argument('--http-port',type=int);p.add_argument('--bearer-token-env');a=p.parse_args();serve(a.workspace,a.http_port,a.bearer_token_env)
if __name__=='__main__':main()
