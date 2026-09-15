import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from openai_budget import Budget, Stopped
from openai_gateway import Gateway, validate


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = {'enabled':True, 'active_run':'luna', 'models':['gpt-5.6-luna'],
                        'parent_model':'gpt-5.6-luna', 'root_session':'root', 'maximum_inflight':1}
        self.body = {'model':'gpt-5.6-luna','stream':True,'input':[{'role':'user','content':'test'}],
                     'reasoning':{'effort':'max'}}

    def test_request_restrictions_and_max_effort(self):
        out = validate(dict(self.body), self.control, 'root')
        self.assertEqual(out['reasoning']['effort'], 'max')
        self.assertEqual(out['service_tier'], 'default')
        self.assertEqual(out['max_output_tokens'], 128000)
        for change in ({'model':'gpt-6-astra'}, {'service_tier':'fast'},
                       {'previous_response_id':'resp_unknown'}, {'max_output_tokens':128001},
                       {'tools':[{'type':'web_search'}]}, {'reasoning':{'mode':'pro'}}):
            with self.assertRaises(Stopped): validate({**self.body, **change}, self.control, 'root')
        with self.assertRaises(Stopped): validate(self.body, self.control, 'second_parent')

    def fake_gateway(self, terminal=True):
        g = object.__new__(Gateway)
        g.evidence = self.root
        g.control = lambda: dict(self.control)
        g.budget = Budget(self.root/'budget.json')
        g.budget.create()
        g.lock = threading.Lock()
        g.active = {'root'}
        class Connection:
            def close(self): pass
        def connect(endpoint, body):
            if endpoint == 'responses/input_tokens':
                return Connection(), io.BytesIO(b'{"input_tokens":100}')
            self.assertEqual(body['reasoning']['effort'], 'max')
            self.assertEqual(g.budget.snapshot()['requests'][0]['status'], 'pending')
            event = {'type':'response.completed','response':{'id':'resp_test',
                'model':'gpt-5.6-luna', 'service_tier':'default',
                'usage':{'input_tokens':100,'output_tokens':200,
                    'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}}}
            data = ('data: '+json.dumps(event)+'\n\n').encode() if terminal else b''
            return Connection(), io.BytesIO(data)
        g.connect = connect
        return g

    def test_terminal_reaches_client_only_after_settlement(self):
        g = self.fake_gateway()
        received = []
        def emit(chunk):
            if chunk and b'response.completed' in chunk:
                self.assertEqual(g.budget.snapshot()['requests'][0]['status'], 'settled')
            received.append(chunk)
        g.upstream(validate(self.body,self.control,'root'),self.control,'root',emit)
        self.assertIsNone(received[-1])
        self.assertEqual(g.budget.snapshot()['committed_and_reserved_nano'], 260000)

    def test_connection_without_usage_stops_with_full_reservation(self):
        g = self.fake_gateway(False)
        g.upstream(validate(self.body,self.control,'root'),self.control,'root',lambda _:None)
        s = g.budget.snapshot()
        self.assertEqual(s['requests'][0]['status'], 'uncertain')
        self.assertEqual(s['committed_and_reserved_nano'], s['requests'][0]['reserved_nano'])
        self.assertEqual(s['stopped'],'provider_stream_ended_without_usage')

    def test_no_generation_when_disk_reserve_reached(self):
        g = self.fake_gateway()
        with patch('openai_gateway.shutil.disk_usage') as disk:
            disk.return_value.free = 0
            g.upstream(validate(self.body,self.control,'root'),self.control,'root',lambda _:None)
        self.assertEqual(g.budget.snapshot()['requests'], [])
        self.assertEqual(g.budget.snapshot()['stopped'], 'disk_reserve_reached')


if __name__ == '__main__': unittest.main()
