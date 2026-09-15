"""Host boundary regressions, with no credentials or model calls."""
import importlib.util
import contextlib
import io
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prime_rpc_check', ROOT/'tools/prime_rpc_check.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.campaign = {
            'workspace': '/isolated/public', 'profile': '/isolated/profile',
            'native_home': '/home/test-agent', 'agent_user': 'test-agent',
            'prime_root': '/opt/prime-agent-0.9.3', 'lab_root': '/opt/compression-lab-0.5.1',
            'native_prefix': '/opt/native', 'mcp_url': 'http://127.0.0.1:8774/mcp',
            'parent_model': 'openai-codex/gpt-6-astra', 'parent_thinking': 'medium',
            'child_model': 'openai-codex/gpt-5.6-luna', 'child_thinking': 'high',
            'maximum_children': 1, 'maximum_depth': 1,
        }

    def command(self, **kwargs):
        self.assertTrue(callable(getattr(driver, 'build_command', None)),
                        'The packaged RPC runner has no configurable, recovery-safe launch boundary')
        return driver.build_command(self.campaign, **kwargs)

    def test_fresh_launch_retains_native_session_and_official_messaging(self):
        command = self.command()
        self.assertEqual(command[command.index('--model')+1], 'gpt-6-astra')
        self.assertEqual(command[command.index('--thinking')+1], 'medium')
        self.assertEqual(command[command.index('--session-dir')+1], '/home/test-agent/.prime/agent/sessions')
        self.assertNotIn('--no-session', command)
        self.assertEqual(command[command.index('--skill')+1],
                         '/opt/prime-agent-0.9.3/node_modules/prime-agent/skills/agent-message')
        self.assertIn('COMPRESSION_LAB_PRIME_PROFILE=/isolated/profile', command)
        self.assertIn('COMPRESSION_LAB_RELEASE_ROOT=/opt/compression-lab-0.5.1', command)

    def test_resume_does_not_override_retained_child_model_or_effort(self):
        saved = '/home/test-agent/.prime/agent/sessions/retained.jsonl'
        command = self.command(resume_session=saved)
        self.assertEqual(command[command.index('--resume')+1], saved)
        for option in ('--model', '--provider', '--thinking', '--session-dir', '--no-session'):
            self.assertNotIn(option, command)
        self.assertIn('--skill', command)

    def test_commissioned_kernel_and_launcher_are_preserved_without_source_rewriting(self):
        self.campaign.update(prime_launcher='/protected/launch.sh',kernel_python='/protected/kernel-4g.sh')
        command=self.command()
        self.assertIn('/protected/launch.sh',command)
        self.assertIn('PRIME_AGENT_KERNEL_PYTHON=/protected/kernel-4g.sh',command)
        self.campaign['kernel_python']='relative/kernel.sh'
        with self.assertRaises(ValueError): self.command()

    def test_usage_and_quota_error_do_not_export_account_details(self):
        self.assertTrue(callable(getattr(driver, 'visible', None)),
                        'Sanitization must also cover startup diagnostics')
        event = {'type': 'message_end', 'message': {'role': 'assistant',
                 'stopReason': 'error', 'errorMessage': 'usage_limit_reached account details secret',
                 'content': [{'type': 'thinking', 'thinking': 'private'},
                             {'type': 'text', 'text': 'Visible status'}],
                 'usage': {'input': 7, 'output': 2}, 'accountId': 'secret'}}
        visible = driver.visible(event)
        self.assertEqual(visible['message']['usage'], {'input': 7, 'output': 2})
        self.assertEqual(visible['message']['content'], [{'type': 'text', 'text': 'Visible status'}])
        self.assertNotIn('secret', json.dumps(visible))


class IdleRecoveryTests(unittest.TestCase):
    def test_fresh_stop_survives_missing_rpc_end_but_old_or_active_messages_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); profile = root/'profile'; profile.mkdir()
            native = profile/'session.jsonl'
            campaign = {'profile': str(profile), 'native_home': '/native'}
            verified = {'session_id': 'root', 'session_file': '/native/session.jsonl'}
            entries = [{'type': 'session', 'id': 'root'},
                       {'type': 'message', 'id': 'final', 'timestamp': '2026-09-11T06:00:02+00:00',
                        'message': {'role': 'assistant', 'stopReason': 'stop',
                                    'content': [{'type': 'thinking', 'thinking': 'private'}]}}]
            def write(rows):
                native.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            def state():
                return {'status': 'running', 'verified_session': verified,
                        'last_prompt_at': '2026-09-11T06:00:01+00:00'}
            write(entries)
            idle = {'isStreaming': False, 'isCompacting': False}
            receipt = driver.recovered_idle(campaign, state(), idle)
            self.assertEqual(receipt['message_id'], 'final')
            self.assertNotIn('private', json.dumps(receipt))
            self.assertIsNone(driver.recovered_idle(campaign, state(), {**idle, 'isStreaming': True}))
            self.assertIsNone(driver.recovered_idle(campaign, state(), {**idle, 'isCompacting': True}))
            self.assertIsNone(driver.recovered_idle(campaign, {**state(), 'last_prompt_at': '2026-09-11T06:00:03+00:00'}, idle))
            for message in ({'type': 'message', 'message': {'role': 'toolResult'}},
                            {'type': 'custom_message', 'customType': 'agent_message'}):
                write(entries+[message])
                self.assertIsNone(driver.recovered_idle(campaign, state(), idle))
            write(entries)
            with native.open('a') as stream:stream.write('{"type":"message"')
            self.assertIsNone(driver.recovered_idle(campaign, state(), idle))
            write(entries)
            self.assertIsNone(driver.recovered_idle(campaign, {**state(), 'verified_session': {**verified, 'session_id': 'other'}}, idle))
            self.assertIsNone(driver.recovered_idle(campaign, {**state(), 'verified_session': {**verified, 'session_file': '/native/../session.jsonl'}}, idle))


class NativeShutdownTests(unittest.TestCase):
    def test_unresponsive_native_process_is_still_killed_after_grace(self):
        process = subprocess.Popen([sys.executable, '-c',
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'print("ready",flush=True); time.sleep(60)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        selector = selectors.DefaultSelector()
        try:
            self.assertEqual(process.stdout.readline(), b'ready\n')
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            receipt = driver.shutdown_native(process, selector, lambda command: None, grace_seconds=.05)
            self.assertEqual(receipt, {'method': 'SIGKILL', 'returncode': -signal.SIGKILL})
        finally:
            if process.poll() is None: process.kill()
            process.wait()
            selector.close()
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()


class RunnerProcessTests(unittest.TestCase):
    def test_rpc_probes_recover_missing_end_and_preserve_new_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); profile = root/'profile'; profile.mkdir()
            output = root/'driver'; config = root/'campaign.json'
            config.write_text(json.dumps({
                'status': 'research_running', 'deadline_utc': None, 'driver_output': str(output),
                'parent_model': 'openai-codex/gpt-6-astra', 'parent_thinking': 'medium',
                'maximum_children': 0, 'child_model': None,
                'profile': str(profile), 'native_home': '/native'}))
            config.chmod(0o600)
            fixture = root/'rpc.py'
            fixture.write_text('''import datetime,json,sys
from pathlib import Path
path=Path(sys.argv[1]);path.write_text(json.dumps({'type':'session','id':'root'})+'\\n')
turn=0
for line in sys.stdin:
 c=json.loads(line)
 if c['type']=='get_state':
  print(json.dumps({'type':'response','command':'get_state','success':True,'data':{
   'model':{'id':'gpt-6-astra','provider':'openai-codex'},'thinkingLevel':'medium',
   'sessionId':'root','sessionFile':'/native/session.jsonl','isStreaming':False,
   'isCompacting':False,'messageCount':turn}}),flush=True)
 elif c['type']=='prompt':
  turn+=1
  print(json.dumps({'type':'agent_start'}),flush=True)
  message={'type':'message','id':str(turn),'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'message':{'role':'assistant','stopReason':'stop'} if turn==1 else {'role':'user'}}
  with path.open('a') as out:out.write(json.dumps(message)+'\\n')
 elif c['type']=='abort':break
''')
            captured = []; failures = []
            def monitor():
                def wait_for(predicate):
                    until = time.monotonic()+5
                    while time.monotonic()<until:
                        try:status = json.loads((output/'status.json').read_text())
                        except (OSError, ValueError):status = {}
                        if predicate(status):return status
                        time.sleep(.01)
                    raise AssertionError('RPC lifecycle did not reach expected observation')
                def send(commands):
                    with (output/'commands.jsonl').open('a') as out:
                        for command in commands:out.write(json.dumps(command)+'\n')
                try:
                    wait_for(lambda s:s.get('status')=='ready')
                    for turn in (1,2):
                        send([{'type':'prompt','message':'fixture'}, {'type':'get_state'}])
                        captured.append(wait_for(lambda s:s.get('native_lifecycle',{}).get('messageCount')==turn))
                except BaseException as error:failures.append(error)
                finally:send([{'type':'stop_driver'}])
            watcher = threading.Thread(target=monitor);watcher.start()
            with patch.object(sys, 'argv', ['runner','--campaign',str(config),'--probe-only']), \
                 patch.object(driver, 'build_command', return_value=[sys.executable,str(fixture),str(profile/'session.jsonl')]), \
                 contextlib.redirect_stdout(io.StringIO()):
                driver.main()
            watcher.join(1)
            self.assertFalse(failures, failures)
            self.assertEqual([s['status'] for s in captured], ['idle','running'])
            self.assertEqual([s['completed_prompts'] for s in captured], [1,1])
            self.assertEqual(captured[0]['completion_source'], 'persisted_native_stop_and_rpc_quiescence')

    def run_fixture(self, extra_events=(), prior=None, returned_id='root-original', initial_response=None,
                    expect_graceful_close=False, capture_ready=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root/'campaign.json'
            output = root/'driver'
            config.write_text(json.dumps({
                'status': 'research_running', 'deadline_utc': None,
                'driver_output': str(output), 'parent_model': 'openai-codex/gpt-6-astra',
                'parent_thinking': 'medium', 'maximum_children': 1,
                'child_model': 'openai-codex/gpt-5.6-luna',
            }))
            config.chmod(0o600)
            response = {'type': 'response', 'command': 'get_state', 'success': True,
                        'data': {'model': {'id': 'gpt-6-astra', 'provider': 'openai-codex'},
                                 'thinkingLevel': 'medium', 'sessionId': returned_id,
                                 'sessionFile': '/native/saved.jsonl'}}
            if initial_response is not None:
                response = initial_response
            fixture = root/'rpc_fixture.py'
            fixture.write_text('import sys,json\n'
                               'for line in sys.stdin:\n'
                               ' command=json.loads(line)\n'
                               ' if command.get("type")=="get_state":\n'
                               f'  events=json.loads({json.dumps(json.dumps([response, *extra_events]))})\n'
                               '  for event in events:print(json.dumps(event),flush=True)\n'
                               + (' elif command.get("type")=="abort":pass\n'
                                'import time\ntime.sleep(.1)\n'
                                'print("x"*131072,flush=True)\n'
                                f'open({str(root/"graceful-close")!r},"w").write("closed")\n'
                                if expect_graceful_close else ' elif command.get("type")=="abort":break\n'))
            args = ['prime_rpc_check.py', '--campaign', str(config), '--seconds', '1', '--probe-only']
            if prior:
                saved = root/'prior.json'; saved.write_text(json.dumps(prior))
                args += ['--resume-session', '/native/saved.jsonl', '--prior-status', str(saved)]
            # Only the external native provider is replaced; real JSONL pipes,
            # process shutdown, event filtering and durable status are exercised.
            ready = []
            def wait_ready():
                until = time.monotonic()+1
                while time.monotonic()<until:
                    try: observed=json.loads((output/'status.json').read_text())
                    except (OSError,ValueError): observed={}
                    if observed.get('status')=='ready':
                        ready.append(observed)
                        with (output/'commands.jsonl').open('a') as out:
                            out.write(json.dumps({'type':'stop_driver'})+'\n')
                        return
                    time.sleep(.01)
            watcher=threading.Thread(target=wait_ready)
            with patch.object(sys, 'argv', args), patch.object(
                    driver, 'build_command', return_value=[sys.executable, str(fixture)]), contextlib.redirect_stdout(io.StringIO()):
                if capture_ready: watcher.start()
                driver.main()
            if capture_ready:
                watcher.join(1)
                self.assertEqual(len(ready),1,'stopped native session never became ready')
                self.assertEqual(ready[0]['completed_prompts'],prior['completed_prompts'])
            if expect_graceful_close:
                self.assertEqual((root/'graceful-close').read_text(), 'closed')
            return json.loads((output/'status.json').read_text()), (output/'events.jsonl').read_text()

    def test_stop_delivers_eof_and_drains_output_before_forcing_process_exit(self):
        status, events = self.run_fixture([{'type': 'response', 'command': 'get_state',
            'success': False, 'error': 'Timed out waiting for native state'}], expect_graceful_close=True)
        self.assertEqual(status['status'], 'native_state_error')
        self.assertNotIn('x'*1000, events)

    def test_stopped_resume_waits_for_host_input_despite_old_prompt_timestamp(self):
        status, _ = self.run_fixture(prior={'status':'experiment_stopping','completed_prompts':1,
            'last_prompt_at':'2026-09-12T09:58:42+00:00',
            'verified_session':{'session_id':'root-original','session_file':'/native/saved.jsonl'}},
            capture_ready=True)
        self.assertEqual(status['status'],'stopped')
        self.assertEqual(status['completed_prompts'],1)

    def test_open_ended_run_stops_on_real_protocol_quota_and_sanitizes_error(self):
        status, events = self.run_fixture([{'type': 'message_end', 'message': {
            'role': 'assistant', 'stopReason': 'error',
            'errorMessage': 'usage_limit_reached secret-account-details',
            'content': [{'type': 'thinking', 'thinking': 'private-thought'}]}}])
        self.assertEqual(status['status'], 'allowance_exhausted')
        self.assertTrue(status['model_effort_match'])
        self.assertNotIn('secret-account-details', events)
        self.assertNotIn('private-thought', events)

    def test_resume_rejects_a_different_native_root_before_research(self):
        prior = {'verified_session': {'session_id': 'root-original', 'session_file': '/native/saved.jsonl'},
                 'children': {'retained': {'model': 'openai-codex/gpt-5.6-luna'}}}
        status, _ = self.run_fixture(prior=prior, returned_id='different-root')
        self.assertEqual(status['status'], 'session_identity_or_model_mismatch')
        self.assertEqual(list(status['children']), ['retained'])

    def test_failed_state_poll_preserves_identity_and_private_error_fingerprint(self):
        status, events = self.run_fixture([{
            'id': 'lifecycle-failed', 'type': 'response', 'command': 'get_state',
            'success': False, 'error': 'Timed out waiting for daemon response: secret-account-details',
        }])
        self.assertEqual(status['status'], 'native_state_error')
        self.assertEqual(status['verified_session']['session_id'], 'root-original')
        self.assertTrue(status['model_effort_match'])
        self.assertEqual(status['native_state_error']['reason'], 'rpc_timeout')
        self.assertEqual(len(status['native_state_error']['error_sha256']), 64)
        self.assertEqual(status['completed_prompts'], 0)
        self.assertNotIn('secret-account-details', events + json.dumps(status))
        self.assertIn('lifecycle-failed', events)

    def test_initial_state_failure_never_verifies_a_session(self):
        status, _ = self.run_fixture(initial_response={
            'type': 'response', 'command': 'get_state', 'success': False,
            'error': 'The isolated session worker stopped during this command',
        })
        self.assertEqual(status['status'], 'native_state_error')
        self.assertNotIn('verified_session', status)
        self.assertEqual(status['native_state_error']['reason'], 'rpc_worker_stopped')

    def test_rejected_input_stops_without_counting_completion_or_exporting_error(self):
        for command in ('prompt', 'steer', 'follow_up'):
            with self.subTest(command=command):
                status, events = self.run_fixture([{
                    'id': 'rejected-feedback', 'type': 'response', 'command': command,
                    'success': False, 'error': 'Cannot admit a session action while queued session input is suspended. secret-account-details',
                }], expect_graceful_close=True)
                self.assertEqual(status['status'], 'native_prompt_error')
                self.assertEqual(status['verified_session']['session_id'], 'root-original')
                self.assertEqual(status['completed_prompts'], 0)
                receipt = status['native_prompt_error']
                self.assertIs(receipt['success'], False)
                self.assertEqual(receipt['id'], 'rejected-feedback')
                self.assertEqual(receipt['command'], command)
                self.assertEqual(len(receipt['error_sha256']), 64)
                self.assertNotIn('secret-account-details', events + json.dumps(status))

    def test_input_acceptance_is_not_a_rejection_or_completed_turn(self):
        status, _ = self.run_fixture([{'id': 'accepted', 'type': 'response',
            'command': 'prompt', 'success': True}])
        self.assertNotIn('native_prompt_error', status)
        self.assertEqual(status['completed_prompts'], 0)

    def test_malformed_state_fails_closed_without_discarding_identity(self):
        for data in (None, [], {}, {'model': []}, {
                'model': {'id': 'gpt-6-astra', 'provider': 'openai-codex'},
                'thinkingLevel': 'medium', 'sessionId': 'root-original'}):
            with self.subTest(data=data):
                status, _ = self.run_fixture([{'type': 'response', 'command': 'get_state',
                                              'success': True, 'data': data}])
                self.assertEqual(status['status'], 'native_state_error')
                self.assertEqual(status['native_state_error']['reason'], 'malformed_state')
                self.assertEqual(status['verified_session']['session_id'], 'root-original')

    def test_fresh_session_is_pinned_and_actual_changes_remain_fatal(self):
        original = {'model': {'id': 'gpt-6-astra', 'provider': 'openai-codex'},
                    'thinkingLevel': 'medium', 'sessionId': 'root-original',
                    'sessionFile': '/native/saved.jsonl'}
        for changed in ({'sessionId': 'different-root'}, {'sessionFile': '/native/other.jsonl'},
                        {'model': {'id': 'other-model', 'provider': 'openai-codex'}},
                        {'thinkingLevel': 'high'}):
            with self.subTest(changed=changed):
                status, _ = self.run_fixture([{'type': 'response', 'command': 'get_state',
                                              'success': True, 'data': {**original, **changed}}])
                self.assertEqual(status['status'], 'session_identity_or_model_mismatch')
                self.assertEqual(status['verified_session']['session_id'], 'root-original')
                self.assertEqual(status['verified_session']['model'], 'gpt-6-astra')
                self.assertEqual(status['verified_session']['thinking'], 'medium')
                self.assertEqual(status['verified_session']['session_file'], '/native/saved.jsonl')

    def test_second_child_is_detected_against_retained_lifetime_roster(self):
        prior = {'verified_session': {'session_id': 'root-original', 'session_file': '/native/saved.jsonl'},
                 'children': {'retained': {'model': 'openai-codex/gpt-5.6-luna'}}}
        status, _ = self.run_fixture([{'type': 'rlm_child_update', 'child': {
            'id': 'second', 'model': 'openai-codex/gpt-5.6-luna', 'status': 'running'}}], prior=prior)
        self.assertEqual(status['status'], 'delegation_policy_violation')
        self.assertEqual(set(status['children']), {'retained', 'second'})


if __name__ == '__main__':
    unittest.main()
