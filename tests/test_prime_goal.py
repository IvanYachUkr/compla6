"""Exercise the actual runner's JSONL subprocess loop without paid inference."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from test_controller import EvidenceEngine, protocol
from compression_lab.controller import Controller
import prime_rpc_check as driver
from prime_goal import GoalBridge, require_gateway_retry_owner


class GoalRunnerTests(unittest.TestCase):
    def test_metered_native_profile_cannot_multiply_gateway_retries(self):
        from compression_lab.util import Error
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'.prime/agent/settings.json';path.parent.mkdir(parents=True)
            for retry in ({'enabled':True,'provider':{'maxRetries':0}},
                          {'enabled':False,'provider':{'maxRetries':6}}, {}):
                path.write_text(json.dumps({'retry':retry}))
                with self.assertRaises(Error) as caught:
                    require_gateway_retry_owner({'profile':tmp})
                self.assertEqual(caught.exception.code,'native_retries_must_be_disabled')
            path.write_text(json.dumps({'retry':{'enabled':False,'provider':{'maxRetries':0}}}))
            require_gateway_retry_owner({'profile':tmp})

    def test_ambiguous_delivery_stops_for_reconciliation_without_resending(self):
        from compression_lab.util import Error
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); engine=EvidenceEngine(root/'workspace');profile=root/'profile';profile.mkdir()
            (profile/'session.jsonl').write_text(json.dumps({'type':'session','id':'native-root'})+'\n')
            c=Controller.configure(engine,{**protocol(),'schema_version':2,
                'deadline_epoch':None,'max_feedback_attempts':None,'max_failure_continuations':3})
            campaign={'workspace':str(engine.root),'parent_model':'provider/root','parent_thinking':'medium',
                'maximum_children':1,'maximum_depth':1,'child_model':'provider/worker','child_thinking':'high',
                'profile':str(profile),'native_home':'/native'}
            with patch('prime_goal.Engine',return_value=engine):bridge=GoalBridge(campaign)
            verified={'session_id':'native-root','session_file':'/native/session.jsonl'}
            bridge.bind(verified);bridge.observe('running');bridge.observe('idle')
            c.claim_delivery(c.pending_deliveries()[0]['id'])
            state={'status':'idle','verified_session':verified}
            with self.assertRaises(Error) as caught:
                bridge.poll(state,lambda _:self.fail('ambiguous feedback must not be resent'))
            self.assertEqual(caught.exception.code,'native_delivery_ambiguous')
            self.assertTrue(state['delivery_reconciliation_required'])
            self.assertEqual(c.state()['failure_continuations'],1)

    def test_accepted_finish_waits_for_the_native_final_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=EvidenceEngine(tmp)
            c=Controller.configure(engine,{**protocol(),'schema_version':2,
                'deadline_epoch':None,'max_feedback_attempts':None,'max_failure_continuations':0})
            campaign={'workspace':tmp,'parent_model':'provider/root','parent_thinking':'medium',
                'maximum_children':1,'maximum_depth':1,'child_model':'provider/worker','child_thinking':'high'}
            with patch('prime_goal.Engine',return_value=engine): bridge=GoalBridge(campaign)
            verified={'session_id':'native-root'};bridge.bind(verified);bridge.observe('running')
            self.assertTrue(c.finish('finished',outcome='negative')['accepted'])
            state={'status':'running','verified_session':verified}
            bridge.poll(state,lambda _:self.fail('unexpected new prompt'))
            self.assertEqual(state['status'],'running')
            self.assertTrue(state['completion_pending_handoff'])
            state['status']='idle'
            bridge.poll(state,lambda _:self.fail('unexpected new prompt'))
            self.assertEqual(state['status'],'experiment_closed')

    def test_natural_language_failure_gets_three_real_followups_then_closes(self):
        self.run_failed_rounds(False)

    def test_resume_recovers_completed_feedback_without_spending_another_round(self):
        self.run_failed_rounds(True)

    def test_rejected_feedback_drains_and_preserves_unacknowledged_delivery(self):
        self.run_failed_rounds(False, reject_feedback=True)

    def run_failed_rounds(self, resume, reject_feedback=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); profile=root/'profile'; profile.mkdir()
            output=root/'driver'; config=root/'campaign.json'
            engine=EvidenceEngine(root/'workspace')
            controller=Controller.configure(engine,{**protocol(),'schema_version':2,
                'deadline_epoch':None,'max_feedback_attempts':None,'max_failure_continuations':3})
            config.write_text(json.dumps({'status':'research_running','deadline_utc':None,
                'driver_output':str(output),'workspace':str(engine.root),
                'parent_model':'provider/root','parent_thinking':'medium','maximum_children':1,
                'maximum_depth':1,'child_model':'provider/worker','child_thinking':'high',
                'profile':str(profile),'native_home':'/native'}))
            config.chmod(0o600)
            prior=root/'prior-status.json'
            if resume:
                import datetime
                controller.attach_backend('prime-jsonl-rpc','0.9.3')
                controller.admit('root-launch','root',None,'provider/root','medium')
                controller.bind_session('root','native-root','provider/root','medium')
                controller.observe('start','root','running',0)
                controller.observe('end','root','idle',1)
                message=controller.claim_delivery(controller.pending_deliveries()[0]['id'])
                controller.observe('restart','root','running',2)
                timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
                entries=[{'type':'session','id':'native-root'}]
                for i,text in enumerate(('Initial research',GoalBridge.marker(message['id'])+'Retry failed research')):
                    for role,content in [('user',text),('assistant','Failed again')]:
                        entries.append({'type':'message','id':role+str(i),'timestamp':timestamp,
                            'message':{'role':role,'stopReason':'stop','content':[{'type':'text','text':content}]}})
                (profile/'session.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))
                prior.write_text(json.dumps({'status':'running','last_prompt_at':timestamp,'completed_prompts':1,
                    'verified_session':{'session_id':'native-root','session_file':'/native/session.jsonl'}}))
            fixture=root/'rpc.py'
            fixture.write_text('''import datetime,json,sys
from pathlib import Path
path=Path(sys.argv[1])
if not path.exists():path.write_text(json.dumps({'type':'session','id':'native-root'})+'\\n')
n=sum(json.loads(line).get('message',{}).get('role')=='user' for line in path.read_text().splitlines())
for line in sys.stdin:
 c=json.loads(line)
 if c['type']=='get_state':
  print(json.dumps({'type':'response','command':'get_state','success':True,'data':{
   'model':{'id':'root','provider':'provider'},'thinkingLevel':'medium','sessionId':'native-root',
   'sessionFile':'/native/session.jsonl','isStreaming':False,'isCompacting':False}}),flush=True)
 elif c['type']=='prompt':
  if sys.argv[2]=='reject' and c.get('id','').startswith('msg-'):
   print(json.dumps({'type':'response','id':c['id'],'command':'prompt','success':False,
    'error':'Cannot admit a session action while queued session input is suspended.'}),flush=True)
   continue
  n+=1
  with path.open('a') as out:
   for role,text in [('user',c['message']),('assistant','Research complete. Failed to meet the speed floor.')]:
    out.write(json.dumps({'type':'message','id':role+str(n),'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),
     'message':{'role':role,'stopReason':'stop','content':[{'type':'text','text':text}]}})+'\\n')
  print(json.dumps({'type':'agent_start'}),flush=True)
  print(json.dumps({'type':'agent_end'}),flush=True)
 elif c['type']=='abort':break
''')
            failures=[]
            def initial_prompt():
                try:
                    until=time.monotonic()+5
                    while time.monotonic()<until:
                        try: state=json.loads((output/'status.json').read_text())
                        except (OSError,ValueError):state={}
                        if state.get('status')=='ready':
                            with (output/'commands.jsonl').open('a') as out:
                                out.write(json.dumps({'type':'prompt','message':'Do the commissioned research.'})+'\n')
                            return
                        time.sleep(.01)
                    raise AssertionError('runner did not become ready')
                except BaseException as ex:failures.append(ex)
            watcher=threading.Thread(target=initial_prompt)
            arguments=['runner','--campaign',str(config),'--seconds','8']
            if resume:arguments+=['--resume-session','/native/session.jsonl','--prior-status',str(prior)]
            with patch('prime_goal.Engine', return_value=engine), \
                 patch.object(sys,'argv',arguments), \
                 patch.object(driver,'build_command',return_value=[sys.executable,str(fixture),str(profile/'session.jsonl'),
                                                                  'reject' if reject_feedback else 'accept']), \
                 contextlib.redirect_stdout(io.StringIO()):
                if not resume:watcher.start()
                driver.main()
                if not resume:watcher.join(1)
            self.assertFalse(failures,failures)
            state=json.loads((output/'status.json').read_text())
            if reject_feedback:
                self.assertEqual(state['status'],'native_prompt_error')
                self.assertEqual(state['completed_prompts'],1)
                self.assertEqual(controller.state()['failure_continuations'],1)
                self.assertEqual(controller.state()['completion']['stop_reason'],'infrastructure_blocked')
                self.assertTrue(controller.state()['completion']['drain_complete'])
                self.assertEqual(len(controller.unacknowledged_deliveries()),1)
                self.assertEqual(sum(json.loads(line).get('message',{}).get('role')=='user'
                    for line in (profile/'session.jsonl').read_text().splitlines()),1)
                return
            self.assertEqual(state['status'],'experiment_closed')
            self.assertEqual(state['completed_prompts'],4)
            self.assertEqual(controller.state()['failure_continuations'],3)
            self.assertEqual(controller.state()['completion']['stop_reason'],'attempt_limit')
            self.assertEqual(len(controller.state()['outbox']),3)
            self.assertTrue(all(m['status']=='acknowledged' for m in controller.state()['outbox'].values()))


if __name__=='__main__':unittest.main()
