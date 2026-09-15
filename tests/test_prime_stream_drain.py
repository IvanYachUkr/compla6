"""A slow host controller must not prevent draining native RPC output."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('drain_runner', ROOT/'tools/prime_rpc_check.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class StreamDrainTests(unittest.TestCase):
    def test_large_ignored_event_does_not_starve_final_handoff(self):
        # Real pipes and JSON parsing; replace only the external native process
        # and controller IPC, whose per-request latency creates the regression.
        class Bridge:
            bound = False
            def __init__(self, campaign): self.controller = self
            def bind(self, actual): self.bound = True
            def poll(self, state, send):
                time.sleep(.02)
                if state['status'] == 'idle': state['status'] = 'experiment_closed'
            def stopped(self, reason): pass
            def state(self): return {'completion':{'accepted':True}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root/'driver'
            config = root/'campaign.json'
            config.write_text(json.dumps({'status':'research_running', 'deadline_utc':None,
                'driver_output':str(output), 'parent_model':'openai/gpt-5.6-terra',
                'parent_thinking':'max', 'maximum_children':0, 'child_model':None}))
            config.chmod(0o600)
            response = {'type':'response', 'command':'get_state', 'success':True,
                'data':{'model':{'id':'gpt-5.6-terra','provider':'openai'}, 'thinkingLevel':'max',
                        'sessionId':'root', 'sessionFile':'/native/session.jsonl'}}
            fixture = root/'native.py'
            fixture.write_text('import json,sys\n'
                'sys.stdin.readline()\n'
                f'print({json.dumps(json.dumps(response))},flush=True)\n'
                'print(json.dumps({"type":"agent_start"}),flush=True)\n'
                'print(json.dumps({"type":"message_update","delta":"x"*(8*1024*1024)}),flush=True)\n'
                'print(json.dumps({"type":"agent_end"}),flush=True)\n'
                'for line in sys.stdin:\n'
                ' if json.loads(line).get("type")=="abort":break\n')
            with patch.object(sys, 'argv', ['runner','--campaign',str(config),'--seconds','3']), \
                 patch.object(driver, 'build_command', return_value=[sys.executable,str(fixture)]), \
                 patch.dict(sys.modules, {'prime_goal':types.SimpleNamespace(GoalBridge=Bridge)}), \
                 contextlib.redirect_stdout(io.StringIO()):
                driver.main()
            status = json.loads((output/'status.json').read_text())
            self.assertEqual(status['status'], 'experiment_closed')
            self.assertEqual(status['completed_prompts'], 1)
            self.assertEqual(status['native_shutdown']['method'], 'eof')
            self.assertNotIn('x'*1000, (output/'events.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
