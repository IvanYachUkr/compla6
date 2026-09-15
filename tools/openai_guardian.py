"""Continuous local checks; only the owner supervisor admits or advances runs."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
from openai_budget import Budget, atomic


def unit_state(name):
    result = subprocess.run(['systemctl','show',name,'-p','ActiveState','-p','MainPID'],
                            capture_output=True,text=True,check=True)
    return dict(line.split('=',1) for line in result.stdout.splitlines())


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True);a=p.parse_args()
    control=a.base/'control';budget=Budget(control/'budget.json')
    while True:
        try:
            state=json.loads((control/'state.json').read_text())
            spending=budget.snapshot();free=shutil.disk_usage(a.base).free
            report={'checked_epoch':time.time(),'free_bytes':free,
                    'committed_and_reserved_nano':spending['committed_and_reserved_nano'],
                    'remaining_nano':spending['remaining_nano'],'budget_stop':spending['stopped'],
                    'active_run':state['active_run'],'commission_status':state['status']}
            fault=None
            if state['status']=='running' and state['active_run']:
                entry=state['runs'][state['active_run']]
                campaign=json.loads(Path(entry['campaign']).read_text())
                units={k:unit_state(campaign[k+'_unit']) for k in ('driver','evaluator')}
                report['units']=units
                path=Path(campaign['driver_output'])/'status.json'
                native=json.loads(path.read_text()) if path.exists() else {}
                report['native_status']=native.get('status')
                report['tool_calls']=native.get('tool_calls',0)
                report['children']=native.get('children',{})
                completed = native.get('status')=='experiment_closed' and (
                    native.get('completion',{}).get('accepted') is True or
                    native.get('completion',{}).get('drain_complete') is True)
                if completed:
                    gateway=json.loads((control/'gateway.json').read_text())
                    if gateway.get('enabled'):
                        gateway['enabled']=False
                        atomic(control/'gateway.json',gateway)
                    report['run_complete_waiting_owner_audit']=True
                    receipt=control/('completion-'+state['active_run']+'.json')
                    if not receipt.exists():atomic(receipt,native['completion'])
                    atomic(control/'guardian.json',report)
                    time.sleep(2)
                    continue
                if spending['stopped']:fault=spending['stopped']
                elif free<16*1024**3:fault='disk_reserve_reached'
                elif any(v['ActiveState'] in ('failed','inactive') for v in units.values()):
                    fault='owned_research_service_stopped'
                elif native.get('status') in ('provider_error','model_mismatch','native_state_error','native_prompt_error',
                       'delegation_policy_violation','session_identity_or_model_mismatch','exited'):
                    fault='native_'+native['status']
                elif len(native.get('children',{}))>campaign['maximum_children']:
                    fault='native_child_count_violation'
                if fault:
                    budget.stop(fault)
                    gateway=json.loads((control/'gateway.json').read_text())
                    gateway['enabled']=False
                    atomic(control/'gateway.json',gateway)
                    receipt={'reason':fault,'detected_epoch':time.time(),'run':state['active_run'],
                             'free_bytes':free,'native_status':native.get('status')}
                    stop_path=control/('stop-'+state['active_run']+'.json')
                    if not stop_path.exists():atomic(stop_path,receipt)
                    subprocess.run(['systemctl','stop',campaign['driver_unit'],campaign['evaluator_unit']],check=True)
                    report['stopped_owned_services']=True
            atomic(control/'guardian.json',report)
            if state['status'] in ('complete','closed'):
                return
        except Exception as exc:
            # Failure to read supervision state must close API admission.
            try:budget.stop('guardian_'+type(exc).__name__)
            except Exception:pass
            atomic(control/'guardian-error.json',{'error_type':type(exc).__name__,'at_epoch':time.time()})
            raise
        time.sleep(2)


if __name__=='__main__':main()
