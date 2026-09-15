"""Trusted goal bridge for the isolated Prime RPC runner.

The driver owns this object; it never runs in the model's Python kernel. Lab
evidence owns completion, and the private gateway owns paid-request admission.
"""
import json
import os
import inspect
import subprocess
from pathlib import Path

from compression_lab.controller import Controller
from compression_lab.engine import Engine
from compression_lab.util import Error
from openai_budget import Budget
from prime_observer import refresh_native_children


def require_gateway_retry_owner(campaign):
    settings = json.loads((Path(campaign['profile'])/'.prime/agent/settings.json').read_text())
    retry = settings.get('retry', {})
    if retry.get('enabled') is not False or retry.get('provider', {}).get('maxRetries') != 0:
        raise Error('native_retries_must_be_disabled', 'The metered gateway owns all capacity retries')


class EvaluatorController:
    """Keep controller files and cancellation operations under the evaluator UID."""
    def __init__(self, campaign, uid):
        self.command = ['sudo','-n','-u','#'+str(uid),
            str(Path(campaign['lab_root'])/'venv/bin/python'),'-m','compression_lab.cli',
            'controller-host','--workspace',campaign['workspace']]

    def __getattr__(self, operation):
        def call(*args, **kwargs):
            values = inspect.signature(getattr(Controller, operation)).bind(None,*args,**kwargs)
            arguments = {k:v for k,v in values.arguments.items() if k!='self'}
            result = subprocess.run([*self.command,'--operation',operation,'--arguments','-'],
                input=json.dumps(arguments),text=True,capture_output=True,timeout=30)
            try: response = json.loads(result.stdout)
            except ValueError: raise Error('invalid_controller_host_response') from None
            if result.returncode: raise Error((response.get('reason_codes') or ['controller_host_failed'])[0])
            return response['metrics']
        return call


class GoalBridge:
    def __init__(self, campaign):
        self.campaign = campaign
        uid = Path(campaign['workspace']).stat().st_uid
        if uid != os.geteuid():
            if os.geteuid()!=0: raise Error('controller_requires_evaluator_identity')
            self.controller = EvaluatorController(campaign, uid)
        else:
            self.controller = Controller(Engine(campaign['workspace']))
        state = self.controller.state(); protocol = state['protocol']
        if protocol['schema_version'] != 2: raise Error('completion_protocol_v2_required')
        expected = {'root':[{'model':campaign['parent_model'],'effort':campaign['parent_thinking']}],
                    'worker':([{'model':campaign['child_model'],'effort':campaign['child_thinking']}]
                              if campaign['maximum_children'] else [])}
        if (protocol['models'] != expected or protocol['max_children'] != campaign['maximum_children']
                or protocol['max_depth'] != campaign['maximum_depth']): raise Error('campaign_controller_mismatch')
        if protocol.get('max_cost_nano') is not None: require_gateway_retry_owner(campaign)
        self.bound = False
        self.refresh_spending()
        self.controller.reconcile()
        self.controller.guard_research()
        self.controller.attach_backend('prime-jsonl-rpc', '0.9.3')
        if not state['nodes']:
            self.controller.admit('root-launch', 'root', None, campaign['parent_model'], campaign['parent_thinking'])
        elif set(n['node_id'] for n in state['nodes'].values() if n['parent_id'] is None) != {'root'}:
            raise Error('unexpected_root_node')

    def refresh_spending(self):
        c = self.controller; state = c.state(); cap = state['protocol'].get('max_cost_nano')
        if cap is None: return
        path = Path(self.campaign['budget_ledger'])
        st = path.stat()
        if path.is_symlink() or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise Error('unsafe_budget_ledger')
        snapshot = Budget(path).snapshot()
        if snapshot['cap_nano'] != cap: raise Error('meter_cap_mismatch')
        stopped = snapshot['stopped']
        if stopped and stopped != 'budget_cannot_cover_next_request':
            c.stop('infrastructure_blocked')
            return
        receipt = dict(committed_nano=snapshot['committed_and_reserved_nano'],
                       admission_blocked=bool(stopped),
                       pending_requests=sum(r['status']=='pending' for r in snapshot['requests']))
        old = state.get('spending')
        if not old or any(old[k] != value for k,value in receipt.items()):
            c.record_spending(**receipt, sequence=old['sequence']+1 if old else 0)

    def bind(self, verified):
        self.controller.bind_session('root', verified['session_id'],
                                     self.campaign['parent_model'], self.campaign['parent_thinking'])
        self.bound = True

    def observe(self, status, node_id='root'):
        if not self.bound: return
        node = self.controller.state()['nodes'][node_id]
        if node['status'] != status:
            sequence = node['sequence']+1
            self.controller.observe(node_id+'-'+str(sequence), node_id, status, sequence)

    def acknowledge(self, verified):
        pending = self.controller.unacknowledged_deliveries()
        if not pending: return
        profile = Path(self.campaign['profile']).resolve(strict=True)
        relative = Path(verified['session_file']).relative_to(self.campaign['native_home'])
        path = (profile/relative).resolve(strict=True)
        if not path.is_relative_to(profile): raise Error('native_path_escape')
        markers = {self.marker(m['id']):m['id'] for m in pending}
        session = None
        with path.open() as source:
            for line in source:
                if not line.endswith('\n'): break
                entry = json.loads(line)
                if entry.get('type') == 'session': session = entry.get('id')
                message = entry.get('message', {})
                if session != verified['session_id'] or message.get('role') != 'user': continue
                content = message.get('content', [])
                text = content if isinstance(content, str) else ''.join(
                    part.get('text', '') for part in content if part.get('type')=='text')
                for marker, message_id in markers.items():
                    if text.startswith(marker) and entry.get('id'):
                        self.controller.ack_delivery(message_id, session+':'+entry['id'])

    @staticmethod
    def marker(message_id): return '[compression-lab feedback '+message_id+']\n'

    def poll(self, state, send):
        self.refresh_spending()
        if not self.bound: return
        self.acknowledge(state['verified_session'])
        if state['status']=='idle' and self.controller.unacknowledged_deliveries():
            state['delivery_reconciliation_required'] = True
            raise Error('native_delivery_ambiguous', 'Idle native session has no receipt for claimed feedback')
        if state.get('children'):
            observed = refresh_native_children(self.campaign, state)
            for child_id, child in observed['children'].items():
                nodes = self.controller.state()['nodes']
                if child_id not in nodes:
                    self.controller.admit('child-'+child_id, child_id, 'root',
                                          self.campaign['child_model'], self.campaign['child_thinking'])
                if child.get('native_observation_error'):
                    self.observe('running', child_id)
                    continue
                self.controller.bind_session(child_id, child['native_session_id'],
                                             child['native_model'], child['native_thinking'])
                self.observe('completed' if child.get('native_idle_receipt') and not child.get('activity')
                             else 'running', child_id)
        if state['status'] in ('running','idle'): self.observe(state['status'])
        view = self.controller.reconcile()
        state['completion'] = view['completion']
        state['completion_limits'] = view['completion_limits']
        if (view['lifecycle']=='closed' and state['status']=='running'
                and view['completion'].get('stop_reason')=='explicit_finish'):
            # finish is a tool call inside the current native turn. Let the model
            # receive its acceptance receipt and complete its final handoff.
            state['completion_pending_handoff'] = True
            return
        if view['lifecycle'] in ('closed','stopping'):
            state['status'] = 'experiment_closed' if view['lifecycle']=='closed' else 'experiment_stopping'
            return
        if state['status'] != 'idle': return
        for pending in self.controller.pending_deliveries():
            self.controller.guard_research()
            delivery = self.controller.claim_delivery(pending['id'])
            message = self.marker(delivery['id'])+json.dumps(delivery['message'])
            limits = self.controller.state()['protocol'].get('max_failure_continuations')
            used = self.controller.state()['failure_continuations']
            if limits is not None and used == limits:
                message += '\nThis is the last allowed continuation. Complete the research and lab checks, then submit finish and write the final handoff, including a negative result if specifications still fail.'
            # Claim is durable before writing. A crash leaves an ambiguous receipt;
            # restart checks the native user message and never blindly resends it.
            self.observe('running')
            send({'id':delivery['id'],'type':'prompt','message':message})
            state['status'] = 'running'
            break

    def stopped(self, reason):
        self.controller.stop(reason)
        for node in self.controller.state()['nodes'].values():
            self.observe('stopped', node['node_id'])
