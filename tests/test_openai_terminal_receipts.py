import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import io
import json
import unittest

from openai_gateway import validate
import test_openai_gateway as test_gateway


class TerminalReceiptTests(unittest.TestCase):
    setUp = test_gateway.GatewayTests.setUp
    fake_gateway = test_gateway.GatewayTests.fake_gateway

    def run_terminal(self, event):
        gateway = self.fake_gateway()
        original_connect = gateway.connect

        def connect(endpoint, body):
            connection, response = original_connect(endpoint, body)
            if endpoint == 'responses':
                response = io.BytesIO(('data: ' + json.dumps(event) + '\n\n').encode())
            return connection, response

        gateway.connect = connect
        gateway.upstream(validate(self.body, self.control, 'root'), self.control,
                         'root', lambda _: None)
        return gateway.budget.snapshot()

    def event(self, kind, usage):
        return {'type': kind, 'response': {
            'id': 'resp_failed', 'model': 'gpt-5.6-luna',
            'status': 'failed' if kind == 'response.failed' else 'completed',
            'service_tier': 'default', 'usage': usage,
            'error': {'code': 'unclassified_provider_failure', 'message': 'private echo'},
            'output': [{'content': 'private model content'}]}}

    def test_failed_response_without_usage_keeps_full_reservation(self):
        state = self.run_terminal(self.event('response.failed', None))
        request = state['requests'][0]
        self.assertEqual(request['status'], 'uncertain')
        self.assertEqual(state['stopped'], 'provider_response_failed_without_usage')
        self.assertEqual(state['committed_and_reserved_nano'], request['reserved_nano'])
        receipt = json.loads((self.root/'terminal-00001.json').read_text())
        self.assertEqual(receipt['response']['id'], 'resp_failed')
        self.assertIsNone(receipt['response']['usage'])
        self.assertEqual(receipt['response']['error']['code'], 'unclassified_provider_failure')
        self.assertNotIn('private', json.dumps(receipt))

    def test_invalid_completed_usage_is_preserved_before_rejection(self):
        usage = {'input_tokens': 999999, 'output_tokens': 200,
                 'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}
        state = self.run_terminal(self.event('response.completed', usage))
        request = state['requests'][0]
        self.assertEqual(request['status'], 'violation')
        self.assertEqual(state['committed_and_reserved_nano'], request['reserved_nano'])
        receipt = json.loads((self.root/'terminal-00001.json').read_text())
        self.assertEqual(receipt['response']['usage'], usage)

    def test_failed_response_with_valid_usage_is_charged_then_stopped(self):
        usage = {'input_tokens': 100, 'output_tokens': 200,
                 'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}
        state = self.run_terminal(self.event('response.failed', usage))
        self.assertEqual(state['requests'][0]['status'], 'settled')
        self.assertEqual(state['committed_and_reserved_nano'], 260000)
        self.assertEqual(state['stopped'], 'provider_response_failed')
        receipt = json.loads((self.root/'terminal-00001.json').read_text())
        self.assertEqual(receipt['event_type'], 'response.failed')


if __name__ == '__main__':
    unittest.main()
