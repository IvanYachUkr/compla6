"""Fault-injected provider transport; no credentials, network, or real sleeps."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import openai_gateway as transport
from openai_budget import Budget, Stopped


def event(kind, response=None, **kw):
    return ('data: '+json.dumps({'type':kind,**({'response':response} if response else {}),**kw})+'\n\n').encode()


class CapacityRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.g=transport.Gateway.__new__(transport.Gateway)
        self.g.evidence=root;self.g.budget=Budget(root/'ledger.json');self.g.budget.create()
        self.control={'enabled':True,'active_run':'run-one','root_session':'root'}
        self.g.control=lambda:dict(self.control)
        self.body={'model':'gpt-5.6-sol','max_output_tokens':10,'reasoning':{'effort':'max'},'service_tier':'default'}
        self.success={'id':'success','model':'gpt-5.6-sol','status':'completed','service_tier':'default',
                      'usage':{'input_tokens':1,'output_tokens':1}}
        self.failed={'id':'failure','model':'gpt-5.6-sol','status':'failed','usage':None,
                     'error':{'type':'service_unavailable_error','code':'server_is_overloaded'}}

    def run_transport(self, failures=6, failed_sse=False, fault=None):
        calls=[];delays=[];chunks=[]
        def connect(endpoint, body):
            if endpoint.endswith('input_tokens'):return io.BytesIO(),io.BytesIO(b'{"input_tokens":1}')
            calls.append(endpoint)
            if len(calls)<=failures:
                if fault:raise fault
                if failed_sse:
                    return io.BytesIO(),io.BytesIO(event('response.created',{'id':'failed-prefix'})+
                        event('response.output_text.delta',delta='MUST NOT REACH CLIENT')+
                        event('response.failed',self.failed))
                raise transport.CapacityError('server_is_overloaded')
            return io.BytesIO(),io.BytesIO(event('response.completed',self.success))
        with patch.object(self.g,'connect',side_effect=connect), \
             patch.object(self.g,'wait_retry',side_effect=lambda delay,control:delays.append(delay)), \
             patch.object(transport.shutil,'disk_usage',return_value=type('Disk',(),{'free':100*1024**3})()):
            self.g.upstream(self.body,self.control,'root',chunks.append)
        return calls,delays,b''.join(c for c in chunks if c)

    def test_exact_six_delays_and_separate_charged_reservations(self):
        calls,delays,data=self.run_transport()
        self.assertEqual(len(calls),7)
        self.assertEqual(delays,[15,30,60,120,180,300])
        state=self.g.budget.snapshot()
        self.assertIsNone(state['stopped'])
        self.assertEqual([r['status'] for r in state['requests']],['capacity_hold']*6+['settled'])
        self.assertIn(b'"id": "success"',data)
        self.assertGreater(state['committed_and_reserved_nano'],state['requests'][-1]['charged_nano'])

    def test_failed_sse_content_is_discarded_before_retry(self):
        calls,delays,data=self.run_transport(failures=1,failed_sse=True)
        self.assertEqual(len(calls),2)
        self.assertEqual(delays,[15])
        self.assertNotIn(b'MUST NOT REACH CLIENT',data)
        self.assertNotIn(b'failed-prefix',data)
        self.assertEqual(json.loads((self.g.evidence/'terminal-00001.json').read_text())['response']['usage'],None)

    def test_seventh_capacity_failure_stops_without_an_eighth_request(self):
        calls,delays,data=self.run_transport(failures=99)
        self.assertEqual(len(calls),7)
        self.assertEqual(delays,[15,30,60,120,180,300])
        self.assertEqual(self.g.budget.snapshot()['stopped'],'capacity_retries_exhausted')
        self.assertIn(b'capacity_retries_exhausted',data)

    def test_authentication_and_unclassified_errors_are_not_retried(self):
        calls,delays,_=self.run_transport(fault=Stopped('upstream_responses_HTTP_401'))
        self.assertEqual(len(calls),1)
        self.assertEqual(delays,[])
        self.assertEqual(self.g.budget.snapshot()['requests'][0]['status'],'uncertain')

    def test_budget_caps_all_retries_including_unknown_charges(self):
        with self.g.budget.locked():
            state=self.g.budget.load();state['cap_nano']=21_000_000
            transport.atomic(self.g.budget.path,state)
        calls,delays,_=self.run_transport()
        self.assertEqual(len(calls),1)
        state=self.g.budget.snapshot()
        self.assertLessEqual(state['committed_and_reserved_nano'],state['cap_nano'])
        self.assertEqual(state['stopped'],'budget_cannot_cover_next_request')

    def test_backoff_is_cancelled_when_owner_disables_the_campaign(self):
        self.control['enabled']=False
        with self.assertRaisesRegex(Stopped,'campaign_not_enabled'):
            self.g.wait_retry(15,self.control)

    def test_http_classification_retries_capacity_but_not_quota_or_slowdown(self):
        self.g.key='fixture-unused-key'
        for status,code,retry in ((503,'server_is_overloaded',True),
                                  (503,'slow_down',False),(429,'insufficient_quota',False),(401,'invalid_api_key',False)):
            with self.subTest(status=status,code=code), patch.object(transport.http.client,'HTTPSConnection') as connection:
                response=connection.return_value.getresponse.return_value
                response.status=status
                response.read.return_value=json.dumps({'error':{'code':code,'type':'service_unavailable_error'}}).encode()
                with self.assertRaises(Stopped) as error: self.g.connect('responses',{})
                self.assertEqual(isinstance(error.exception,transport.CapacityError),retry)
                self.assertTrue(connection.return_value.close.called)

    def test_valid_usage_on_failed_capacity_attempt_is_charged(self):
        self.failed['usage']=self.success['usage']
        calls,_,_=self.run_transport(failures=1,failed_sse=True)
        self.assertEqual(len(calls),2)
        state=self.g.budget.snapshot()
        self.assertEqual([r['status'] for r in state['requests']],['settled','settled'])
        self.assertEqual(state['committed_and_reserved_nano'],sum(r['charged_nano'] for r in state['requests']))

    def test_existing_violation_cannot_be_reclassified_or_bypassed(self):
        ticket=self.g.budget.reserve('old','old','gpt-5.6-sol',4097,10,'old')
        with self.assertRaises(Stopped):
            self.g.budget.settle(ticket,{},response_id='old',response_model='gpt-5.6-sol',service_tier='default')
        calls,_,_=self.run_transport()
        self.assertEqual(calls,[])
        self.assertEqual(self.g.budget.snapshot()['requests'][0]['status'],'violation')


if __name__=='__main__':unittest.main()
