"""Host-owned experiment lifecycle, admission and durable delivery receipts.

Only brief and finish are public operations. Native adapters call the remaining
methods from the trusted host, never through the agent's public MCP endpoint.
"""
from __future__ import annotations
import math
import time
from .util import Error, Ledger, digest, ident, lock, now, save
from .policy import PublicPolicy

ACTIVE = {'admitted', 'running', 'idle'}


def positive(value, name, zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise Error('invalid_'+name)
    return value


class Controller:
    def __init__(self, engine):
        if hasattr(engine, 'researcher_view'): engine=engine.researcher_view()
        self.engine = engine
        self.ledger = Ledger(engine.root/'controller')
        if not self.ledger.journal.exists(): raise Error('controller_not_configured')

    @staticmethod
    def exists(engine): return (engine.root/'controller'/'events.jsonl').exists()

    @classmethod
    def configure(cls, engine, protocol):
        if hasattr(engine, 'researcher_view'): engine=engine.researcher_view()
        if (engine.root/'owner-policy.json').exists(): raise Error('private_workspace_forbidden')
        allowed = {'schema_version', 'objective', 'deadline_epoch',
                   'max_children', 'max_depth', 'models', 'max_feedback_attempts',
                   'memory_snapshot', 'refinement_mode', 'stop_on_success'}
        version = protocol.get('schema_version')
        if version == 2: allowed |= {'time_limit_seconds', 'max_failure_continuations', 'max_cost_nano'}
        if set(protocol)-allowed or version not in (1, 2): raise Error('invalid_protocol')
        if protocol.get('objective') not in ('qualification', 'improvement'): raise Error('invalid_objective')
        if type(protocol.get('stop_on_success', False)) is not bool: raise Error('invalid_stop_policy')
        if engine.metadata()[0].get('baseline_visibility','visible')=='hidden' and protocol['objective']!='qualification':
            raise Error('hidden_baseline_requires_qualification')
        deadline = protocol.get('deadline_epoch')
        if version==2 and protocol.get('time_limit_seconds') is not None:
            positive(protocol['time_limit_seconds'], 'time_limit_seconds')
            if deadline is not None: raise Error('duplicate_time_limit')
            deadline = time.time()+protocol['time_limit_seconds']
        if (deadline is not None or version == 1) and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= time.time()):
            raise Error('invalid_deadline')
        protocol = {**protocol, 'deadline_epoch': deadline}
        if version == 2:
            if protocol.get('max_feedback_attempts') is not None: raise Error('use_max_failure_continuations')
            protocol['max_feedback_attempts'] = None
            for key in ('max_failure_continuations', 'max_cost_nano'):
                if protocol.get(key) is not None: positive(protocol[key], key, zero=key=='max_failure_continuations')
            if all(protocol.get(key) is None for key in ('deadline_epoch', 'max_failure_continuations', 'max_cost_nano')):
                raise Error('completion_limit_required')
        for key in ('max_children', 'max_depth', *(['max_feedback_attempts'] if version == 1 else [])):
            positive(protocol.get(key), key, zero=True)
        if set(protocol.get('models', {})) != {'root', 'worker'}: raise Error('invalid_models')
        for role, rows in protocol['models'].items():
            if not isinstance(rows, list) or (role == 'root' and not rows): raise Error('invalid_models')
            for row in rows:
                if set(row) != {'model', 'effort'} or any(not isinstance(v, str) or not v or len(v)>200 for v in row.values()):
                    raise Error('invalid_models')
        from .refinement import RefinementStore
        snapshot=RefinementStore(engine).snapshot()
        if snapshot['context'] is None: raise Error('memory_context_unavailable')
        if 'memory_snapshot' in protocol and protocol['memory_snapshot']!=snapshot: raise Error('memory_snapshot_mismatch')
        if protocol.get('refinement_mode','adaptive') not in ('adaptive','frozen'): raise Error('invalid_refinement_mode')
        protocol={**protocol,'memory_snapshot':snapshot,'refinement_mode':protocol.get('refinement_mode','adaptive')}
        state = engine.state()
        Ledger(engine.root/'controller').create({
            'stage': 'public_search', 'run_id': state['run_id'], 'card_digest': state['card_digest'],
            'runtime_digest': state['runtime_digest'], 'protocol': protocol, 'protocol_digest': digest(protocol),
            'lifecycle': 'open', 'created_at': now(), 'backend': None, 'nodes': {}, 'requests': {},
            'events': {}, 'outbox': {},
            'feedback_attempts': 0, 'failure_continuations': 0, 'completion': None})
        return cls(engine)

    def state(self):
        state = self.ledger.read()
        native = self.engine.state()
        if digest(state['protocol']) != state['protocol_digest'] or any(state[k] != native[k] for k in ('run_id','card_digest','runtime_digest')):
            raise Error('controller_context_mismatch')
        return state

    def attach_backend(self, name, version):
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version: raise Error('invalid_backend')
        def change(s):
            self._open(s)
            attestation = {'name':name, 'version':version}
            if s['backend'] is not None and s['backend'] != attestation: raise Error('backend_identity_mismatch')
            s['backend'] = attestation
            return s
        return self.ledger.update('attach_backend', change)['backend']

    @staticmethod
    def _open(s):
        if s['lifecycle'] != 'open': raise Error('controller_closed')
        deadline = s['protocol']['deadline_epoch']
        if deadline is not None and time.time() >= deadline: raise Error('deadline_reached')

    def guard_research(self):
        state = self.state(); self._open(state)
        if state['protocol'].get('max_cost_nano') is not None:
            if state.get('spending') is None: raise Error('spending_unavailable')
            if self._limit_reason(state, evaluations=False) == 'money_limit': raise Error('money_limit')

    def record_spending(self, committed_nano, sequence, admission_blocked=False, pending_requests=0):
        """Trusted aggregate meter, including reservations for all sessions/retries.

        Native SDK cost estimates and model-supplied values are not billing proof.
        The transport independently reserves the same cap before every request.
        """
        positive(committed_nano, 'committed_nano', zero=True)
        positive(sequence, 'sequence', zero=True)
        positive(pending_requests, 'pending_requests', zero=True)
        if type(admission_blocked) is not bool: raise Error('invalid_admission_blocked')
        receipt = dict(committed_nano=committed_nano, sequence=sequence,
                       admission_blocked=admission_blocked, pending_requests=pending_requests)
        def change(s):
            old = s.get('spending')
            if old and sequence <= old['sequence']:
                if sequence == old['sequence'] and receipt != old: raise Error('spending_snapshot_conflict')
                return s
            s['spending'] = receipt
            return s
        return self.ledger.update('spending', change)['spending']

    def _limit_reason(self, state, *, attempts=False, evaluations=True):
        protocol = state['protocol']
        deadline = protocol['deadline_epoch']
        if deadline is not None and time.time() >= deadline: return 'deadline'
        spending = state.get('spending')
        cap = protocol.get('max_cost_nano')
        if cap is not None and spending and (spending['admission_blocked'] or
                (spending['committed_nano'] >= cap and not spending.get('pending_requests'))):
            return 'money_limit'
        if evaluations:
            native = self.engine.state(); budget = native['budgets'].get('agent', {})
            if (budget.get('evaluations', 0) >= native['search_budget']['candidate_evaluations']
                    or budget.get('wall_seconds', 0) >= native['search_budget']['wall_seconds']):
                return 'evaluation_budget'
        limit = protocol.get('max_failure_continuations')
        if attempts and limit is not None and state['failure_continuations'] >= limit:
            return 'attempt_limit'
        return None

    def memory_state(self):
        from .refinement import RefinementStore
        protocol=self.state()['protocol'];initial=protocol['memory_snapshot'];current=None;error=None
        try: current=RefinementStore(self.engine).snapshot()
        except Exception as ex: error=getattr(ex,'code','memory_unavailable')
        return {'mode':protocol['refinement_mode'],'initial_digest':initial['digest'],
                'current_digest':current['digest'] if current else None,
                'initial_versions':initial['entry_versions'],
                'current_versions':current['entry_versions'] if current else None,
                'changed':current is None or current['digest']!=initial['digest'],'error':error}

    def guard_refinement(self,mutation=False):
        memory=self.memory_state()
        if memory['mode']=='frozen' and (mutation or memory['changed']): raise Error('refinement_snapshot_frozen')

    def _request(self, operation, request_id, payload, fn):
        ident(request_id); signature = digest({'operation':operation, 'payload':payload})
        def change(s):
            old = s['requests'].get(request_id)
            if old:
                if old['signature'] != signature: raise Error('request_id_conflict')
                return s
            result = fn(s)
            s['requests'][request_id] = {'signature':signature, 'result':result}
            return s
        return self.ledger.update(operation, change)['requests'][request_id]['result']

    def admit(self, request_id, node_id, parent_id, model, effort):
        ident(node_id)
        if parent_id is not None: ident(parent_id)
        def apply(s):
            self._open(s)
            if s['backend'] is None: raise Error('backend_not_attached')
            if node_id in s['nodes']: raise Error('node_already_exists')
            role = 'root' if parent_id is None else 'worker'
            if {'model':model, 'effort':effort} not in s['protocol']['models'][role]: raise Error('unauthorized_model')
            if parent_id is None:
                if any(n['parent_id'] is None for n in s['nodes'].values()): raise Error('root_already_exists')
                depth = 0
            else:
                parent = s['nodes'].get(parent_id)
                if parent is None or parent['status'] not in ACTIVE: raise Error('parent_not_active')
                depth = parent['depth']+1
                if depth > s['protocol']['max_depth']: raise Error('depth_limit')
                if sum(n['parent_id'] is not None for n in s['nodes'].values()) >= s['protocol']['max_children']: raise Error('child_limit')
            node = {'node_id':node_id, 'parent_id':parent_id, 'model':model, 'effort':effort,
                    'depth':depth, 'status':'admitted', 'sequence':-1, 'native_session_id':None}
            s['nodes'][node_id] = node
            return dict(node)
        return self._request('admit', request_id, [node_id,parent_id,model,effort], apply)

    def bind_session(self, node_id, native_session_id, model, effort):
        ident(node_id); ident(native_session_id)
        def change(s):
            node = s['nodes'].get(node_id)
            if node is None: raise Error('unknown_node')
            if (node['model'],node['effort']) != (model,effort): raise Error('native_identity_mismatch')
            if node['native_session_id'] not in (None,native_session_id): raise Error('native_session_mismatch')
            node['native_session_id'] = native_session_id
            return s
        return self.ledger.update('bind_session', change)['nodes'][node_id]

    def record_usage(self, node_id, native_session_id, input_tokens, output_tokens, sequence):
        """Replace a native session's cumulative own usage; never reserve tokens.

        Input includes cached tokens. Native totals may be corrected later; the
        journal retains those corrections without stopping research.
        """
        for value,name in ((input_tokens,'input_tokens'),(output_tokens,'output_tokens'),(sequence,'sequence')):
            positive(value,name,zero=True)
        def change(s):
            node=s['nodes'].get(node_id)
            if node is None: raise Error('unknown_node')
            if not native_session_id or node['native_session_id'] != native_session_id: raise Error('native_session_mismatch')
            old=node.get('usage')
            if old and sequence <= old['sequence']:
                if sequence==old['sequence'] and (input_tokens,output_tokens)!=(old['input_tokens'],old['output_tokens']):
                    raise Error('usage_snapshot_conflict')
                return s
            node['usage']={'input_tokens':input_tokens,'output_tokens':output_tokens,'sequence':sequence,
                          'corrected':bool(old and (input_tokens<old['input_tokens'] or output_tokens<old['output_tokens']))}
            return s
        return self.ledger.update('native_usage',change)['nodes'][node_id]['usage']

    @staticmethod
    def _usage(state):
        nodes=state['nodes'];reported={key:node['usage'] for key,node in nodes.items() if node.get('usage') is not None}
        inputs=sum(row['input_tokens'] for row in reported.values())
        outputs=sum(row['output_tokens'] for row in reported.values())
        return {'scope':'native_reported_own_usage','input_tokens':inputs,'output_tokens':outputs,
                'total_tokens':inputs+outputs,'reported_nodes':sorted(reported),
                'unreported_nodes':sorted(set(nodes)-set(reported)),
                'corrected_nodes':sorted(key for key,row in reported.items() if row['corrected'])}

    def observe(self, event_id, node_id, status, sequence):
        ident(event_id); positive(sequence, 'sequence', zero=True)
        if status not in ACTIVE|{'completed','failed','stopped'}: raise Error('invalid_native_status')
        payload = [node_id,status,sequence]
        def change(s):
            old = s['events'].get(event_id)
            if old is not None:
                if old != payload: raise Error('event_id_conflict')
                return s
            node = s['nodes'].get(node_id)
            if node is None: raise Error('unknown_node')
            if sequence > node['sequence']:
                node.update(status=status,sequence=sequence)
            s['events'][event_id] = payload
            return s
        self.ledger.update('native_event', change)
        return self.reconcile()

    def _outstanding(self, s, include_root=False):
        children = [n['node_id'] for n in s['nodes'].values() if n['parent_id'] is not None and n['status'] in ACTIVE]
        engine=self.engine.state()
        # A root may request finish from its own active tool turn. Host stops
        # additionally wait for that turn to be aborted outside the tool call.
        roots=[n['node_id'] for n in s['nodes'].values() if include_root and n['parent_id'] is None and n['status']=='running']
        return {'children':children, 'root_turns':roots, 'job_id':engine['active_job'],
                'operation_id':(engine.get('active_operation') or {}).get('id')}

    def brief(self):
        state = self.state(); protocol = state['protocol']; outstanding = self._outstanding(state,state['lifecycle']=='stopping')
        policy = PublicPolicy(self.engine); best,evidence_error = self._best_available(policy)
        try: control=policy.best_control()
        except Exception as ex:control=None;evidence_error=evidence_error or getattr(ex,'code','evidence_unavailable')
        native=self.engine.state();used=native['budgets'].get('agent',{})
        roots = [n for n in state['nodes'].values() if n['parent_id'] is None]
        round_finished = bool(roots and roots[0]['status'] in ('idle','completed','failed'))
        waiting = any(outstanding.values())
        lifecycle = state['lifecycle'] if state['lifecycle']!='open' else 'waiting' if waiting and roots and roots[0]['status']!='running' else 'running' if roots and roots[0]['status']=='running' else 'ready'
        if lifecycle=='stopping': action={'command':'drain','outstanding':outstanding}
        elif evidence_error: action={'command':'inspect_evidence','reason_code':evidence_error}
        else: action = policy.next_action(protocol['objective'], lifecycle=='closed',
                 bool(self._limit_reason(state, attempts=round_finished)), stop_on_success=protocol.get('stop_on_success', False))
        if waiting and action['command'] not in ('status','archive'): action={'command':'wait','outstanding':outstanding}
        return {'schema_version':1,'run_id':state['run_id'],'protocol_digest':state['protocol_digest'],
                'objective':protocol['objective'],'lifecycle':lifecycle,'deadline_epoch':protocol['deadline_epoch'],
                **({'stop_on_success':True} if protocol.get('stop_on_success', False) else {}),
                'usage':self._usage(state),
                **({'completion_limits':{'deadline_epoch':protocol['deadline_epoch'],
                    'max_cost_nano':protocol.get('max_cost_nano'), 'spending':state.get('spending'),
                    'max_failure_continuations':protocol.get('max_failure_continuations'),
                    'failure_continuations':state['failure_continuations'],
                    'exhausted_reason':self._limit_reason(state, attempts=round_finished)}} if protocol['schema_version']==2 else {}),
                'models':protocol['models'],'max_children':protocol['max_children'],'max_depth':protocol['max_depth'],
                'outstanding':outstanding,'best_candidate':best,'next_action':action,
                'current_candidate_digest':native.get('candidate_digest'),
                **({'best_compatible_control':control} if policy.card.get('baseline_visibility','visible')=='visible' else {}),
                'evaluator_budget':{'limit':native['search_budget'],'consumed':used,
                    'remaining_evaluations':max(0,native['search_budget']['candidate_evaluations']-used.get('evaluations',0)),
                    'remaining_active_wall_seconds':max(0,native['search_budget']['wall_seconds']-used.get('wall_seconds',0))},
                'evidence_contract':{'card_digest':state['card_digest'],'runtime_digest':state['runtime_digest'],
                    'resource_profile':policy.pin['resource_profile'],'timing_scope':policy.card.get('timing_policy',{}).get('scope','train-plus-development-v1'),
                    'accounting_policy':policy.card.get('accounting_policy','standalone-v1'),
                    'encode_floor_bytes_per_second':policy.card['objective']['encode_floor_bytes_per_second'],
                    'encoding_floor_scope':policy.card.get('timing_policy',{}).get('encoding_floor_scope','encode'),
                    'full_agent_result_required':True},
                'completion':state['completion'],'stop_request':state.get('stop_request'),
                'evidence_error':evidence_error,'memory':self.memory_state()}

    def reconcile(self):
        if hasattr(self.engine,'recover_operation'): self.engine.recover_operation()
        state = self.state()
        if state['lifecycle']=='closed': return self.brief()
        if state['lifecycle']=='stopping':
            self._drain(); return self.brief()
        if state['protocol']['deadline_epoch'] is not None and time.time() >= state['protocol']['deadline_epoch']:
            self.stop('deadline'); return self.brief()
        strict = state['protocol']['schema_version'] == 2
        reason = self._limit_reason(state) if strict else None
        if reason:
            self.stop(reason); return self.brief()
        view = self.brief()
        roots = [n for n in state['nodes'].values() if n['parent_id'] is None and n['status'] in ('idle','completed','failed')]
        if not roots or view['lifecycle']=='waiting': return view
        target = roots[0]
        if strict:
            # Claimed feedback may not yet have entered the native session.
            if any(m['status'] in ('pending','dispatched') or
                   m.get('from_sequence', -1) >= target['sequence'] for m in state['outbox'].values()): return view
            reason = self._limit_reason(state, attempts=True)
            if reason:
                self.stop(reason); return self.brief()
            if state['protocol'].get('max_cost_nano') is not None and state.get('spending') is None: return view
            action = view['next_action']
            if action['command']=='finish' and action['outcome']=='success':
                self.finish('auto-finish-'+str(target['sequence']), action['candidate_digest'], action['result_id'])
                return self.brief()
        # New evaluator/child/turn evidence creates a new trigger; replaying a
        # poll or acknowledgement cannot create a fresh self-retry.
        trigger = digest({'target':target['node_id'],'sequence':target['sequence'],
                          'results':self.engine.state()['results'],
                          'children':[(n['node_id'],n['sequence'],n['status']) for n in state['nodes'].values() if n['parent_id'] is not None]})
        message_id = 'msg-'+trigger
        def change(s):
            cap = s['protocol']['max_feedback_attempts']
            if s['lifecycle']!='open' or s['nodes'][target['node_id']]['sequence']!=target['sequence']: return s
            if message_id in s['outbox'] or (cap is not None and s['feedback_attempts'] >= cap): return s
            if any(m['status'] in ('pending','dispatched') for m in s['outbox'].values()): return s
            s['outbox'][message_id] = {'id':message_id,'target':target['node_id'],'status':'pending',
                'created_at':now(),'trigger':trigger,'attempts':0,'from_sequence':target['sequence'],
                'failure_continuation':strict and not (view['best_candidate'] and
                    (s['protocol']['objective']=='qualification' or view['best_candidate'].get('baseline_improved') is True)),
                'message':{'type':'controller_feedback','next_action':view['next_action'],
                           'protocol_digest':s['protocol_digest'],
                           **({'completion_limits':view['completion_limits'],
                               'acceptance_criteria':view['evidence_contract']} if strict else {}),
                           'instruction':'Use the recorded next action and exact evidence. A natural-language final answer does not close the experiment.'}}
            s['feedback_attempts']+=1
            return s
        self.ledger.update('reconcile', change)
        return self.brief()

    def pending_deliveries(self): return [m for m in self.state()['outbox'].values() if m['status']=='pending']
    def unacknowledged_deliveries(self): return [m for m in self.state()['outbox'].values() if m['status']=='dispatched']

    def claim_delivery(self, message_id):
        def change(s):
            self._open(s)
            message = s['outbox'].get(message_id)
            if message is None: raise Error('unknown_delivery')
            if message['status'] != 'pending': raise Error('delivery_already_claimed')
            if message.get('failure_continuation'):
                limit = s['protocol'].get('max_failure_continuations')
                if limit is not None and s['failure_continuations'] >= limit: raise Error('attempt_limit')
                s['failure_continuations'] += 1
            message.update(status='dispatched',attempts=message['attempts']+1,dispatched_at=now())
            return s
        return self.ledger.update('claim_delivery', change)['outbox'][message_id]

    def ack_delivery(self, message_id, native_receipt):
        if not isinstance(native_receipt,str) or not native_receipt or len(native_receipt)>300: raise Error('invalid_delivery_receipt')
        def change(s):
            message = s['outbox'].get(message_id)
            if message is None or message['status']=='pending': raise Error('delivery_not_dispatched')
            if message.get('native_receipt') not in (None,native_receipt): raise Error('delivery_receipt_conflict')
            message.update(status='acknowledged',native_receipt=native_receipt)
            return s
        return self.ledger.update('ack_delivery', change)['outbox'][message_id]

    def requeue_rejected_delivery(self, request_id, message_id, dispatch_attempt, native_session_id, rpc_response):
        """Host-reviewed retry of an explicitly rejected native prompt.

        The host matches the raw negative RPC reply to this dispatch attempt
        and retained session before calling. Timeouts or missing acknowledgments
        are not rejection evidence. Keep all prior charges and attempt counters.
        """
        self.state()
        positive(dispatch_attempt, 'dispatch_attempt')
        keys = {'type','id','command','success','error_sha256'}
        if (not isinstance(rpc_response, dict) or set(rpc_response) != keys or
                rpc_response['type'] != 'response' or rpc_response['command'] != 'prompt' or
                rpc_response['id'] != message_id or rpc_response['success'] is not False or
                not isinstance(rpc_response['error_sha256'], str) or
                len(rpc_response['error_sha256']) != 64 or
                any(c not in '0123456789abcdef' for c in rpc_response['error_sha256'])):
            raise Error('invalid_rejection_receipt')
        def apply(s):
            if (s['lifecycle'] != 'closed' or
                    (s.get('completion') or {}).get('stop_reason') != 'infrastructure_blocked'):
                raise Error('recovery_requires_drained_infrastructure_stop')
            if (any(self._outstanding(s, include_root=True).values()) or
                    any(node['status'] in ACTIVE for node in s['nodes'].values())):
                raise Error('recovery_not_quiescent')
            message = s['outbox'].get(message_id)
            if message is None or message['status'] != 'dispatched' or message.get('native_receipt'):
                raise Error('delivery_not_dispatched')
            if message['attempts'] != dispatch_attempt: raise Error('rejection_attempt_mismatch')
            expected = s['nodes'][message['target']]['native_session_id']
            if not expected or native_session_id != expected: raise Error('recovery_context_mismatch')
            receipt = {'request_id':request_id, 'message_id':message_id,
                       'native_session_id':native_session_id, 'rpc_response':rpc_response,
                       'attempt':message['attempts'], 'dispatched_at':message['dispatched_at'],
                       'reviewed_at':now()}
            message.setdefault('rejections', []).append(receipt)
            message['status'] = 'pending'
            message.pop('dispatched_at')
            return receipt
        with lock(self.engine.root/'admission.lock'):
            return self._request('requeue_rejected_delivery', request_id,
                                 [message_id, dispatch_attempt, native_session_id, rpc_response], apply)

    def finish(self, request_id, candidate_digest=None, result_id=None, outcome='success'):
        try:
            with lock(self.engine.root/'operation.lock',nonblocking=True), lock(self.engine.root/'admission.lock'):
                return self._finish(request_id,candidate_digest,result_id,outcome)
        except Error as ex:
            if ex.code!='busy': raise
            return self._finish(request_id,candidate_digest,result_id,outcome,operation_busy=True)

    def _finish(self, request_id, candidate_digest, result_id, outcome, operation_busy=False):
        if outcome not in ('success','negative','infrastructure_blocked'): raise Error('invalid_outcome')
        memory=self.memory_state()
        def apply(s):
            reasons=[]; evidence=None; policy=None
            if s['lifecycle']!='open': reasons.append('controller_closed')
            if operation_busy: reasons.append('operation_in_progress')
            outstanding=self._outstanding(s)
            if any(outstanding.values()): reasons.append('outstanding_work')
            if bool(candidate_digest) != bool(result_id): reasons.append('candidate_and_result_required')
            if result_id:
                try:
                    policy=PublicPolicy(self.engine)
                    evidence=policy.assess(result_id,candidate_digest)
                except Exception as ex: reasons.append(getattr(ex,'code','evidence_unavailable'))
                if evidence and evidence['candidate_digest']!=candidate_digest: reasons.append('candidate_result_mismatch')
            if outcome=='success':
                if not evidence: reasons.append('committed_evidence_required')
                elif not evidence['candidate_qualified']: reasons.extend(evidence['reason_codes'])
                elif s['protocol']['objective']=='improvement' and evidence['baseline_improved'] is not True:
                    reasons.append('baseline_improvement_not_demonstrated')
            best,evidence_error=self._best_available(policy)
            if evidence_error: reasons.append(evidence_error)
            if s['protocol']['schema_version']==2 and outcome!='success':
                if outcome=='infrastructure_blocked': reasons.append('host_stop_required')
                elif best and (s['protocol']['objective']=='qualification' or best.get('baseline_improved') is True):
                    reasons.append('qualified_result_available')
                elif not self._limit_reason(s, attempts=True): reasons.append('completion_budget_remaining')
            receipt={'request_id':request_id,'accepted':not reasons,'requested_outcome':outcome,
                     'protocol_digest':s['protocol_digest'],'created_at':now(),'reason_codes':reasons,
                     'candidate_qualified':bool(best),
                     **({'baseline_improved':best.get('baseline_improved') if best else None}
                        if self.engine.metadata()[0].get('baseline_visibility','visible')=='visible' else {}),
                     'requested_result':evidence,'evidence':best,
                     'memory':memory,'usage_at_finish':self._usage(s),
                     'stop_reason':'explicit_finish' if not reasons else None}
            if not reasons: s.update(lifecycle='closed',completion=receipt)
            return receipt
        return self._request('finish', request_id, [candidate_digest,result_id,outcome], apply)

    def stop(self, reason):
        if reason not in ('deadline','evaluation_budget','operator_stop','infrastructure_blocked','attempt_limit','money_limit'):
            raise Error('invalid_stop_reason')
        def change(s):
            if s['lifecycle']!='open': return s
            s['lifecycle']='stopping'
            s['stop_request']={'stop_reason':reason,'requested_at':now(),
                               'outstanding_at_stop':self._outstanding(s,include_root=True)}
            return s
        # Evaluator admission/dispatch and closure share one short host lock.
        with lock(self.engine.root/'admission.lock'): self.ledger.update('stop', change)
        return self._drain()

    def recover_infrastructure(self, request_id, protocol_digest, native_sessions):
        """Host-only continuation after repairing and draining infrastructure.

        The owner verifies the retained native sessions and refreshes spending
        before this call. No scientific limit, identity or usage is reset.
        """
        self.state()  # Verify the sealed engine/controller context first.
        self.guard_refinement()
        def apply(s):
            if (s['lifecycle'] != 'closed' or
                    (s.get('completion') or {}).get('stop_reason') != 'infrastructure_blocked'):
                raise Error('recovery_requires_drained_infrastructure_stop')
            expected = {key: node['native_session_id'] for key, node in s['nodes'].items()}
            if (protocol_digest != s['protocol_digest'] or not expected or
                    not all(expected.values()) or native_sessions != expected):
                raise Error('recovery_context_mismatch')
            if (any(self._outstanding(s, include_root=True).values()) or
                    any(node['status'] in ACTIVE for node in s['nodes'].values()) or
                    (self.engine.root/'controller'/'cancel.json').exists()):
                raise Error('recovery_not_quiescent')
            spending = s.get('spending')
            if s['protocol'].get('max_cost_nano') is not None and (
                    spending is None or spending.get('pending_requests')):
                raise Error('recovery_spending_unresolved')
            if self._limit_reason(s, attempts=True): raise Error('recovery_limit_exhausted')
            if any(message['status'] == 'dispatched' for message in s['outbox'].values()):
                raise Error('recovery_delivery_ambiguous')
            receipt = {'request_id': request_id, 'recovered_at': now(),
                       'stop_request': s['stop_request'], 'completion': s['completion'],
                       'native_sessions': native_sessions, 'protocol_digest': protocol_digest}
            s.setdefault('recoveries', []).append(receipt)
            s.update(lifecycle='open', completion=None)
            s.pop('stop_request')
            return receipt
        with lock(self.engine.root/'admission.lock'):
            return self._request('recover_infrastructure', request_id,
                                 [protocol_digest, native_sessions], apply)

    def _best_available(self, policy=None):
        try: return (policy or PublicPolicy(self.engine)).best(),None
        except Exception as ex:
            # Broken evidence cannot prevent a safety stop or erase reported usage.
            return None,getattr(ex,'code','evidence_unavailable')

    def _drain(self):
        if hasattr(self.engine,'recover_operation'): self.engine.recover_operation()
        state=self.state()
        if state['lifecycle']=='closed': return state['completion']
        outstanding=self._outstanding(state,include_root=True)
        if outstanding['operation_id']: save(self.engine.root/'controller'/'cancel.json',{'requested_at':now(),'reason':state['stop_request']['stop_reason']})
        if outstanding['job_id']:
            try: self.engine.cancel(outstanding['job_id'])
            except Exception as ex:
                def error(s):
                    s.setdefault('stop_request',{})['cancellation_error']=getattr(ex,'code','cancellation_failed')
                    return s
                self.ledger.update('cancel_error',error)
        if any(outstanding.values()):
            return {'status':'stopping',**state['stop_request'],'outstanding':outstanding}
        best,evidence_error=self._best_available()
        def close(s):
            if s['lifecycle']=='closed': return s
            s['lifecycle']='closed'
            s['completion']={**s['stop_request'],'closed_at':now(),'candidate_qualified':bool(best),
                             **({'baseline_improved':best.get('baseline_improved') if best else None}
                                if self.engine.metadata()[0].get('baseline_visibility','visible')=='visible' else {}),
                             'evidence':best,'evidence_error':evidence_error,
                             'protocol_digest':s['protocol_digest'],'drain_complete':True,
                             'memory':memory,'usage_at_finish':self._usage(s)}
            return s
        memory=self.memory_state()
        return self.ledger.update('drain_complete',close)['completion']
