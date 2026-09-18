"""Machine-readable CLI. All diagnostics/errors are structured and bounded."""
import argparse,json,sys
from pathlib import Path
from .util import Error,canonical,reply,load
from .engine import Engine,ResearcherEngine,doctor
from . import __version__

def parser():
 p=argparse.ArgumentParser(prog='compression-lab');p.add_argument('--version',action='version',version='compression-lab '+__version__);sub=p.add_subparsers(dest='command',required=True)
 p.add_argument('--owner-evidence',action='store_true',help='Trusted host only: include owner baseline evidence; never expose this command or workspace to researchers')
 w=sub.add_parser('strings');w.add_argument('action',choices=['profile','evaluate','submit','status','compare']);w.add_argument('--workspace',required=True);w.add_argument('--candidate');w.add_argument('--results',nargs='+');w.add_argument('--quick',action='store_true');w.add_argument('--job');w.add_argument('--include-columns',action='store_true')
 d=sub.add_parser('doctor');d.add_argument('--release',action='store_true');d.add_argument('--owner')
 i=sub.add_parser('init');i.add_argument('--workspace',required=True);i.add_argument('--dataset',required=True);i.add_argument('--exploratory',action='store_true')
 i=sub.add_parser('card-create');i.add_argument('--dataset-id',required=True);i.add_argument('--input',nargs='+',required=True);i.add_argument('--output',required=True);i.add_argument('--implementation',choices=['open','from_scratch'],default='from_scratch');i.add_argument('--baseline-visibility',choices=['visible','hidden'],default='visible');i.add_argument('--smoke',action='store_true')
 i=sub.add_parser('manifest-template');i.add_argument('--name',default='my-codec')
 for name in ('profile','register','evaluate','status','artifact','cancel','export','baselines','control','compare','resume','inventory','brief','feedback','prompt','record-hypothesis','recipe-create','baseline-trial','experiment-brief','finish','lesson-propose','lesson-check','lesson-list','lesson-rollback','controller-configure','controller-host'):
  a=sub.add_parser(name);a.add_argument('--workspace',required=True)
  if name=='record-hypothesis':a.add_argument('--statement',required=True);a.add_argument('--expected-benefit',required=True);a.add_argument('--falsifier',required=True)
  if name=='controller-configure':a.add_argument('--protocol',required=True)
  if name=='controller-host':a.add_argument('--operation',required=True);a.add_argument('--arguments',default='-')
  if name=='finish':
   a.add_argument('--request-id',required=True);a.add_argument('--candidate');a.add_argument('--result');a.add_argument('--outcome',choices=['success','negative','infrastructure_blocked'],default='success')
  if name=='lesson-propose':a.add_argument('--entry',required=True)
  if name in ('lesson-check','lesson-rollback'):a.add_argument('--lesson',required=True)
  if name=='lesson-check':a.add_argument('--results',nargs=2,required=True)
  if name=='lesson-list':a.add_argument('--limit',type=int,default=8)
  if name=='lesson-rollback':a.add_argument('--revision',type=int,required=True)
  if name=='feedback':a.add_argument('--result',required=True)
  if name=='recipe-create':a.add_argument('--recipe',required=True);a.add_argument('--name',required=True)
  if name=='baseline-trial':a.add_argument('--recipe',required=True);a.add_argument('--depth',choices=['screen','quick','full'],default='screen');a.add_argument('--wait',action='store_true');a.add_argument('--resource-profile')
  if name=='register':a.add_argument('--candidate',required=True)
  if name=='evaluate':
   a.add_argument('--candidate',required=True);a.add_argument('--depth',choices=['screen','quick','full'],default='full');a.add_argument('--wait',action='store_true');a.add_argument('--workload',choices=['independent_objects','static_relational','mutable_store']);a.add_argument('--resource-profile');a.add_argument('--request-id')
  if name=='compare':a.add_argument('--results',nargs='+',required=True);a.add_argument('--diagnostics',action='store_true')
  if name=='status':a.add_argument('--job');a.add_argument('--result')
  if name in ('artifact','export'):a.add_argument('--result',required=True)
  if name=='artifact':a.add_argument('--name',default='raw.json');a.add_argument('--offset',type=int,default=0);a.add_argument('--limit',type=int,default=16384)
  if name=='cancel':a.add_argument('--job',required=True)
  if name=='export':a.add_argument('--output',required=True)
  if name=='baselines':a.add_argument('action',choices=['prepare','list']);a.add_argument('--minimal',action='store_true');a.add_argument('--depth',choices=['screen','quick','full'],default='full');a.add_argument('--resource-profile');a.add_argument('--candidate-result')
  if name=='control':a.add_argument('--method',choices=['random','deterministic'],default='deterministic');a.add_argument('--evaluations',type=int,default=3);a.add_argument('--seed',type=int,default=20260906)
 b=sub.add_parser('baseline-create');b.add_argument('--transform',choices=['identity','delta8','shuffle4'],default='identity');b.add_argument('--link-mode',choices=['shared','static-codec'],default='shared');b.add_argument('--output',required=True);b.add_argument('--family',default='zstd',choices=['stored','zstd','lz4','brotli','xz']);b.add_argument('--level',type=int,default=1)
 for name in ('connect','disconnect'):
  a=sub.add_parser(name);a.add_argument('host',choices=['codex','prime','zcode']);a.add_argument('--project',required=True);a.add_argument('--apply',action='store_true')
 demo=sub.add_parser('demo');demo.add_argument('--output',required=True);demo.add_argument('--matrix',action='store_true')
 for name in ('owner-init','freeze','evaluate-private','disclose'):
  a=sub.add_parser(name);a.add_argument('--owner',required=True)
  if name=='owner-init':a.add_argument('--benchmark-host',action='store_true');a.add_argument('--agent-uid',type=int,required=True);a.add_argument('--private-dataset',required=True);a.add_argument('--workspace',required=True)
  if name=='freeze':a.add_argument('--candidate',required=True)
  if name=='evaluate-private':a.add_argument('--resume',action='store_true')
 return p

def dispatch(a):
 c=a.command
 if c=='strings':
  from . import strings
  if a.action=='profile':return reply(metrics=strings.profile(a.workspace))
  if a.action=='status':return reply(metrics=strings.status(a.workspace,a.job or ''))
  if a.action in ('evaluate','submit'):
   if not a.candidate:raise Error('strings_candidate_required')
   fn=strings.evaluate if a.action=='evaluate' else strings.submit
   return reply(metrics=fn(a.workspace,a.candidate,a.quick))
  return reply(metrics=strings.compare(a.workspace,a.results or [],a.include_columns))
 if c=='doctor':return doctor(a.release,a.owner)
 if c=='init':return Engine.init(a.workspace,a.dataset,a.exploratory).status()
 if c=='card-create':
  from .dataset import create_research_input
  return reply(metrics={'card_path':str(create_research_input(a.output,a.input,a.dataset_id,a.implementation,a.baseline_visibility,a.smoke))})
 if c=='manifest-template':
  from .candidate import blank_manifest
  return reply(metrics={'manifest':blank_manifest(a.name)})
 if c=='baseline-create':
  from .baselines import create
  p=create(Path(a.output),a.family,a.level,transform=a.transform,link_mode=a.link_mode);return reply(metrics={'candidate_path':str(p.absolute())})
 if c in ('connect','disconnect'):
  from .connections import connect,disconnect
  return (connect if c=='connect' else disconnect)(a.host,Path(a.project),a.apply)
 if c=='demo':
  from .demo import run
  return run(Path(a.output),a.matrix)
 if c in ('owner-init','freeze','evaluate-private','disclose'):
  from . import owner
  if c=='owner-init':return owner.init(Path(a.owner),a.agent_uid,Path(a.private_dataset),Path(a.workspace),a.benchmark_host)
  if c=='freeze':return owner.freeze(Path(a.owner),a.candidate)
  if c=='evaluate-private':return owner.evaluate(Path(a.owner),a.resume)
  return owner.disclose(Path(a.owner))
 e=(Engine if a.owner_evidence or c in ('controller-configure','controller-host','lesson-rollback') else ResearcherEngine)(a.workspace)
 if c=='record-hypothesis':return e.record_hypothesis(a.statement,a.expected_benefit,a.falsifier)
 if c=='prompt':
  from .instructions import prompt
  return reply(e.state()['run_id'],metrics={'prompt':prompt(e)})
 if c=='controller-configure':
  from .controller import Controller
  return reply(e.state()['run_id'],metrics=Controller.configure(e,load(a.protocol)).brief())
 if c=='controller-host':
  from .controller import Controller
  operations={'attach_backend','admit','bind_session','record_usage','record_spending','observe','reconcile','stop','recover_infrastructure',
              'state','brief','guard_research','pending_deliveries','unacknowledged_deliveries','claim_delivery','ack_delivery',
              'requeue_rejected_delivery'}
  if a.operation not in operations:raise Error('invalid_host_operation')
  if a.arguments=='-':
   from .util import pairs
   source=sys.stdin.read(65537)
   if len(source)>65536:raise Error('oversized_host_arguments')
   arguments=json.loads(source,object_pairs_hook=pairs)
  else:arguments=load(a.arguments,65536)
  if not isinstance(arguments,dict):raise Error('invalid_host_arguments')
  return reply(e.state()['run_id'],metrics=getattr(Controller(e),a.operation)(**arguments))
 if c=='experiment-brief':return e.experiment_brief()
 if c=='finish':return e.finish(a.request_id,a.candidate,a.result,a.outcome)
 if c=='lesson-propose':return e.lesson_propose(load(a.entry,65536))
 if c=='lesson-check':return e.lesson_check(a.lesson,a.results)
 if c=='lesson-list':return e.lesson_list(a.limit)
 if c=='lesson-rollback':
  from .refinement import RefinementStore
  return reply(e.state()['run_id'],metrics=RefinementStore(e).rollback(a.lesson,a.revision))
 if c=='inventory':return e.inventory()
 if c=='brief':return e.brief()
 if c=='feedback':return e.feedback(a.result)
 if c=='recipe-create':return e.create_recipe(a.recipe,a.name)
 if c=='baseline-trial':return e.baseline_trial(a.recipe,a.depth,a.wait,a.resource_profile)
 if c=='compare':return e.compare(a.results,a.diagnostics)
 if c=='resume':return e.resume()
 if c=='profile':return e.profile()
 if c=='register':return e.register(a.candidate)
 if c=='evaluate':return e.evaluate(a.candidate,a.depth,wait=a.wait,workload=a.workload,resource_profile=a.resource_profile,request_id=a.request_id)
 if c=='status':return e.status(a.job,a.result)
 if c=='artifact':return e.artifact(a.result,a.name,a.offset,a.limit)
 if c=='cancel':return e.cancel(a.job)
 if c=='export':return e.export(a.result,a.output)
 if c=='baselines':return e.baseline_table(a.resource_profile,a.candidate_result) if a.action=='list' else e.prepare_baselines([('stored',0,False),('zstd',1,False)] if a.minimal else None,a.depth,a.resource_profile)
 if c=='control':return e.control(a.method,a.evaluations,a.seed)
 raise Error('unknown_command')

def main():
 try:
  r=dispatch(parser().parse_args());print(canonical(r).decode());return 2 if r.get('status') in ('failed','blocked','error','rejected') else 0
 except (Error,OSError,ValueError,KeyError,TypeError) as ex:
  print(canonical(reply(status='error',reason_codes=[getattr(ex,'code','operation_failed')],metrics={'error':str(ex)[:2000]})).decode());return 2
if __name__=='__main__':sys.exit(main())
