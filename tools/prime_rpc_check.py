#!/usr/bin/env python3
"""Bounded live integration client for Prime's documented JSONL RPC interface.

Append documented RPC commands to output/commands.jsonl. A local `stop_driver`
command closes this client and its isolated process tree. Only completed visible
messages/tool events and compact session statistics are retained; streaming
thinking, authentication, and account records are not evidence artifacts.
"""
import argparse
import datetime
import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path


def build_command(campaign, resume_session=None):
    """Build the trusted launch boundary; saved selections own resumed sessions."""
    prime = Path(campaign['prime_root'])
    lab = Path(campaign['lab_root'])
    profile = Path(campaign['profile'])
    native_home = Path(campaign['native_home'])
    workspace = Path(campaign['workspace'])
    native = Path(campaign['native_prefix'])
    launcher = Path(campaign.get('prime_launcher', lab/'tools/prime_launch_linux.sh'))
    kernel = Path(campaign.get('kernel_python', prime/'kernel-venv/bin/python'))
    if any(not path.is_absolute() for path in (prime, lab, profile, native_home, workspace, native, launcher, kernel)):
        raise ValueError('Campaign runtime, profile and workspace paths must be absolute')
    provider, model = campaign['parent_model'].split('/', 1)
    selection = (['--resume', str(resume_session)] if resume_session else
                 ['--provider', provider, '--model', model, '--thinking', campaign['parent_thinking'],
                  '--session-dir', str(native_home/'.prime/agent/sessions')])
    env = {
        'COMPRESSION_LAB_PRIME_ROOT': str(prime),
        'COMPRESSION_LAB_PRIME_EXECUTABLE': str(prime/'node_modules/prime-agent/dist/bundle/cli.js'),
        'PRIME_AGENT_KERNEL_PYTHON': str(kernel),
        'COMPRESSION_LAB_PRIME_PROFILE': str(profile),
        'COMPRESSION_LAB_RELEASE_ROOT': str(lab),
        'COMPRESSION_LAB_NATIVE_PREFIX': str(native),
        'COMPRESSION_LAB_RUST_TOOLCHAIN': str(native/'rust-1.95.0'),
        'COMPRESSION_LAB_URL': campaign['mcp_url'],
        'RLM_MAX_DEPTH': str(campaign['maximum_depth']),
        'PRIME_AGENT_TELEMETRY': '0', 'PI_SKIP_VERSION_CHECK': '1',
    }
    return ['sudo', '-n', '-u', campaign['agent_user'], '/usr/bin/env',
            *[key+'='+value for key, value in env.items()],
            str(launcher), str(workspace), '--mode', 'rpc', *selection,
            '--no-context-files', '--no-extensions', '--no-skills', '--skill',
            str(prime/'node_modules/prime-agent/skills/agent-message'),
            '--no-prompt-templates', '--no-themes', '--tools', 'ipython', '--offline']


def visible(value):
    if isinstance(value, list):
        return [visible(item) for item in value if not isinstance(item, dict)
                or item.get('type') not in ('thinking', 'redacted_thinking', 'reasoning')]
    if isinstance(value, dict):
        return {key: ('usage_limit_reached: provider account response details omitted'
                      if key in ('errorMessage', 'finalError') and isinstance(item, str)
                      and any(code in item for code in ('usage_limit_reached', 'workspace_member_credits_depleted'))
                      else visible(item)) for key, item in value.items()
                if key not in ('thinking', 'thinkingSignature', 'signature', 'reasoning',
                               'access', 'refresh', 'accountId', 'authorization', 'token')}
    return value


def native_state_response(event):
    """A failed RPC is not an observation of native identity.

    Keep an error fingerprint and a bounded public category, never arbitrary
    provider diagnostics which may contain account or authentication details.
    """
    if event.get('success') is not True:
        error = str(event.get('error') or '')
        reason = ('rpc_timeout' if 'timed out' in error.lower() else
                  'rpc_worker_stopped' if 'worker stopped' in error.lower() else
                  'rpc_connection_closed' if 'connection closed' in error.lower() else
                  'rpc_request_failed')
        return None, {'reason': reason, 'error_sha256': hashlib.sha256(error.encode()).hexdigest()}
    data = event.get('data')
    selected = data.get('model') if isinstance(data, dict) else None
    if not isinstance(selected, dict):
        return None, {'reason': 'malformed_state'}
    actual = {'model': selected.get('id'), 'provider': selected.get('provider'),
              'thinking': data.get('thinkingLevel'), 'session_id': data.get('sessionId'),
              'session_file': data.get('sessionFile')}
    if (not all(isinstance(value, str) and value for value in actual.values())
            or not Path(actual['session_file']).is_absolute()):
        return None, {'reason': 'malformed_state'}
    return actual, None


def shutdown_native(process, selector, send, grace_seconds=10):
    """Let RPC EOF release its native worker lease before bounded escalation."""
    if process.poll() is not None:
        return {'method': 'already_exited', 'returncode': process.returncode}
    try:
        send({'type': 'abort'})
        process.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    method = 'eof'
    for sig, seconds in ((None, grace_seconds), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        if process.poll() is not None:
            break
        if sig is not None:
            method = sig.name
            try: os.killpg(process.pid, sig)
            except ProcessLookupError: pass
        until = time.monotonic() + seconds
        while process.poll() is None and time.monotonic() < until:
            # A native client may finish pending writes before handling EOF.
            # Drain both pipes without publishing interrupted streaming content.
            for key, _ in selector.select(min(.1, max(0, until-time.monotonic()))):
                if not os.read(key.fileobj.fileno(), 65536):
                    selector.unregister(key.fileobj)
    process.wait(timeout=1)
    return {'method': method, 'returncode': process.returncode}


def recovered_idle(campaign, state, native_state):
    """Require both native quiescence and a fresh persisted terminal message."""
    if (state.get('status') != 'running' or native_state.get('isStreaming') is not False
            or native_state.get('isCompacting') is not False):
        return None
    try:
        verified = state['verified_session']
        profile = Path(campaign['profile']).resolve(strict=True)
        relative = Path(verified['session_file']).relative_to(Path(campaign['native_home']))
        path = (profile/relative).resolve(strict=True)
        if not path.is_relative_to(profile):return None
        session_id = None; receipt = None
        # Stream complete records; never retain or export transcript contents.
        with path.open() as source:
            for line in source:
                if not line.endswith('\n'):return None
                entry = json.loads(line)
                if entry.get('type') == 'session':session_id = entry.get('id')
                elif entry.get('type') == 'custom_message' and entry.get('customType') == 'agent_message':receipt = None
                elif entry.get('type') == 'message':
                    message = entry.get('message', {})
                    receipt = ({'session_id':session_id, 'message_id':entry.get('id'), 'at':entry.get('timestamp')}
                               if message.get('role') == 'assistant' and message.get('stopReason') == 'stop' else None)
        if (receipt and receipt['message_id'] and receipt['session_id'] == verified['session_id']
                and datetime.datetime.fromisoformat(receipt['at']) >= datetime.datetime.fromisoformat(state['last_prompt_at'])):
            return receipt
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description='Subscription-supervised Prime RPC runner; no hard aggregate token cap')
    parser.add_argument('--campaign', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='New unused driver directory; defaults to campaign driver_output')
    parser.add_argument('--token-budget', type=int,
                        help='Optional observed ROOT response threshold; may overshoot and omits delayed child usage')
    parser.add_argument('--resume-session', type=Path)
    parser.add_argument('--prior-status', type=Path,
                        help='Required with resume; preserves native root identity and lifetime child registry')
    parser.add_argument('--seconds', type=int, default=0,
                        help='Controller wall limit; 0 uses campaign deadline or authorized subscription supervision')
    parser.add_argument('--probe-only', action='store_true', help='Explicit legacy lifecycle probe; disables goal supervision')
    args = parser.parse_args()
    if args.seconds < 0 or (args.token_budget is not None and args.token_budget <= 0):
        parser.error('Seconds must be nonnegative and an optional token threshold must be positive')
    if bool(args.prior_status) != bool(args.resume_session):
        parser.error('Resume requires both --resume-session and --prior-status')
    stat = args.campaign.stat()
    if not args.campaign.is_file() or args.campaign.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o022:
        parser.error('Campaign must be a caller-owned, non-writable-by-others regular file')
    campaign = json.loads(args.campaign.read_text())
    if campaign.get('status') not in ('prepared-no-model-prompts', 'research_running'):
        parser.error('Campaign does not authorize research')
    deadline = campaign['deadline_utc']
    if deadline is not None:
        remaining = int(datetime.datetime.fromisoformat(deadline).timestamp()-time.time())
        if remaining <= 0:
            parser.error('Campaign deadline has elapsed')
        args.seconds = min(args.seconds, remaining) if args.seconds else remaining
    args.output = args.output or Path(campaign['driver_output'])
    args.model = campaign['parent_model'].split('/', 1)[1]
    args.thinking = campaign['parent_thinking']
    args.output.mkdir(parents=True, exist_ok=False)
    commands = args.output / 'commands.jsonl'
    commands.touch(mode=0o600)
    prior = json.loads(args.prior_status.read_text()) if args.prior_status else {}
    if args.resume_session and not prior.get('verified_session', {}).get('session_id'):
        parser.error('Resume needs a verified prior native root session')
    if args.resume_session and str(args.resume_session) != prior['verified_session'].get('session_file'):
        parser.error('Resume path differs from the verified prior session')
    cmd = build_command(campaign, args.resume_session)
    goal = None
    if not args.probe_only:
        from prime_goal import GoalBridge
        goal = GoalBridge(campaign)
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {}
    for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
        buffers[name] = b''
    started = time.monotonic()
    next_goal_poll = started
    offset = 0
    finished_prompts = prior.get('completed_prompts', 0)
    turn_open = bool(args.resume_session and prior.get('status')=='running' and prior.get('last_prompt_at'))
    next_state_poll = time.monotonic() + 5
    state = {'status': 'starting', 'model': args.model, 'provider': campaign['parent_model'].split('/', 1)[0],
             'thinking': args.thinking, 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'pid': process.pid, 'completed_prompts': finished_prompts, 'tool_calls': 0,
             **({'last_prompt_at':prior['last_prompt_at']} if args.resume_session and prior.get('last_prompt_at') else {}),
             'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'wall_limit_seconds': args.seconds,
             'token_budget': args.token_budget, 'charged_tokens': 0,
             'resumed_session': str(args.resume_session) if args.resume_session else None,
             'prior_status': str(args.prior_status) if args.prior_status else None,
             **({'verified_session': prior['verified_session']} if args.resume_session else {}),
             'children': prior.get('children', {}),
             'token_accounting': 'Subscription-supervised runner, not a strict aggregate token ceiling. Root RPC usage is provisional; native child_usage_attributed receipts and per-child transcripts are required for separated usage. In-flight/retry usage can exceed an optional observed root threshold.'}

    def send(command):
        nonlocal turn_open
        if command.get('type') in ('prompt','steer','follow_up'):
            if goal:
                if not goal.bound: raise RuntimeError('native_identity_not_verified')
                goal.controller.guard_research()
                goal.observe('running')
            state.update(status='running', last_prompt_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            turn_open = True
        process.stdin.write((json.dumps(command) + '\n').encode())
        process.stdin.flush()

    def save_state():
        state['elapsed_seconds'] = time.monotonic() - started
        temp = args.output / '.status.json'
        temp.write_text(json.dumps(state, indent=2) + '\n')
        temp.replace(args.output / 'status.json')

    with (args.output / 'events.jsonl').open('w') as events:
        try:
            send({'id': 'initial-state', 'type': 'get_state'})
            while process.poll() is None and (args.seconds == 0 or time.monotonic() - started < args.seconds):
                with commands.open('rb') as source:
                    source.seek(offset)
                    for line in source:
                        if not line.endswith(b'\n'):
                            break
                        command = json.loads(line)
                        offset += len(line)
                        if command.get('type') == 'stop_driver':
                            state['status'] = 'stopped'
                            return
                        if command.get('type') in ('prompt', 'steer', 'follow_up'):
                            state.update(status='running', last_prompt_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                            turn_open = True
                        send(command)
                if time.monotonic() >= next_state_poll:
                    send({'id': 'lifecycle-' + str(time.monotonic_ns()), 'type': 'get_state'})
                    next_state_poll = time.monotonic() + 5
                for key, _ in selector.select(.2):
                    name = key.data
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[name] += chunk
                    while b'\n' in buffers[name]:
                        line, buffers[name] = buffers[name].split(b'\n', 1)
                        if name == 'stderr':
                            # stderr is bounded startup diagnostics; never dump environment/config.
                            diagnostic = line.decode('utf-8', 'replace')
                            state['last_stderr'] = visible({'errorMessage': diagnostic})['errorMessage'][:1200]
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            state['protocol_error'] = 'non-JSON stdout'
                            continue
                        kind = event.get('type')
                        if kind == 'agent_start':
                            if not turn_open:
                                state['last_prompt_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                            turn_open = True
                            state['status'] = 'running'
                        elif kind == 'agent_end':
                            if turn_open:finished_prompts += 1
                            turn_open = False
                            state.update(status='idle', completed_prompts=finished_prompts)
                            send({'id': 'stats-' + str(finished_prompts), 'type': 'get_session_stats'})
                            event = {'type': kind, 'completed_prompts': finished_prompts}
                        elif kind == 'rlm_child_update':
                            child = event.get('child', {})
                            if child.get('id'):
                                state.setdefault('children', {})[child['id']] = {
                                    k: child[k] for k in ('id', 'sessionName', 'model', 'status', 'sessionDir', 'activity', 'toolUseCount') if k in child}
                                if len(state['children']) > campaign['maximum_children'] or child.get('model') != campaign['child_model']:
                                    state['status'] = 'delegation_policy_violation'
                                    events.write(json.dumps(visible(event)) + '\n'); events.flush()
                                    return
                        elif kind == 'tool_execution_start':
                            state['tool_calls'] += 1
                            state['status'] = 'running'
                        elif kind == 'response' and event.get('command') in ('prompt','steer','follow_up'):
                            if event.get('success') is not True:
                                # A negative admission reply is definitive; absence of a
                                # reply remains ambiguous and must never trigger replay.
                                error = str(event.get('error') or '')
                                receipt = {key:event.get(key) for key in ('type','id','command','success')}
                                receipt['error_sha256'] = hashlib.sha256(error.encode()).hexdigest()
                                state.update(status='native_prompt_error', native_prompt_error=receipt)
                                events.write(json.dumps(receipt) + '\n'); events.flush()
                                return
                        elif kind == 'response' and event.get('command') == 'get_session_stats':
                            metrics = event.get('data', {})
                            state['usage'] = {key: metrics[key] for key in
                                ('tokens', 'cost', 'contextUsage', 'toolCalls', 'assistantMessages') if key in metrics}
                            event = {'type': kind, 'command': 'get_session_stats', 'success': event.get('success'), 'data': state['usage']}
                        elif kind == 'response' and event.get('command') == 'get_state':
                            actual, error = native_state_response(event)
                            if error:
                                state.update(status='native_state_error', native_state_error=error)
                                events.write(json.dumps({'type': kind, 'id': event.get('id'),
                                    'command': 'get_state', 'success': event.get('success'),
                                    'native_state_error': error}) + '\n'); events.flush()
                                return
                            data = event['data']
                            state['model_effort_match'] = (actual['model'] == args.model and
                                actual['provider'] == campaign['parent_model'].split('/', 1)[0] and actual['thinking'] == args.thinking)
                            expected = state.get('verified_session', {})
                            state['session_identity_match'] = all(not expected.get(key) or actual[key] == expected[key]
                                                                 for key in ('session_id', 'session_file'))
                            event = {'type': kind, 'id': event.get('id'), 'command': 'get_state', 'success': True,
                                     'data': actual, 'model_effort_match': state['model_effort_match']}
                            if state['status'] == 'starting':
                                state['status'] = 'running' if turn_open or data.get('isStreaming') is True else 'ready'
                            if not state['model_effort_match'] or not state['session_identity_match']:
                                state['status'] = 'session_identity_or_model_mismatch'
                                state['observed_session'] = actual
                                events.write(json.dumps(visible(event)) + '\n'); events.flush()
                                return
                            state['verified_session'] = actual
                            state['last_verified_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                            if goal: goal.bind(actual)
                            state['native_lifecycle'] = {key:data.get(key) for key in ('isStreaming', 'isCompacting', 'messageCount')}
                            receipt = recovered_idle(campaign, state, data)
                            if receipt:
                                if turn_open:finished_prompts += 1
                                turn_open = False
                                state.update(status='idle', completed_prompts=finished_prompts,
                                             parent_idle_receipt=receipt, completion_source='persisted_native_stop_and_rpc_quiescence')
                                send({'id': 'recovered-stats-' + str(finished_prompts), 'type': 'get_session_stats'})
                        if kind == 'message_end' and event.get('message', {}).get('role') == 'assistant':
                            message = event['message']
                            error = message.get('errorMessage', '')
                            if message.get('stopReason') == 'error' and any(code in error for code in ('usage_limit_reached', 'workspace_member_credits_depleted')):
                                state['status'] = 'allowance_exhausted'
                                state['stop_reason'] = 'Provider subscription allowance exhausted; preserve native parent and child sessions for an authorized continuation.'
                                events.write(json.dumps(visible(event)) + '\n'); events.flush()
                                return
                            if goal and message.get('stopReason') == 'error':
                                state['status'] = 'provider_error'
                                events.write(json.dumps(visible(event)) + '\n'); events.flush()
                                return
                            usage = event['message'].get('usage', {})
                            state['charged_tokens'] += sum(usage.get(k, 0) for k in ('input', 'output', 'cacheWrite'))
                        if kind not in ('response', 'agent_start', 'agent_end', 'message_end',
                                        'tool_execution_start', 'tool_execution_end', 'rlm_child_update', 'auto_retry_start',
                                        'auto_retry_end', 'extension_error'):
                            continue
                        event = visible(event)
                        events.write(json.dumps(event, ensure_ascii=True) + '\n')
                        events.flush()
                        if args.token_budget is not None and state['charged_tokens'] >= args.token_budget:
                            state['status'] = 'observed_root_token_threshold'
                            return
                        if kind in ('agent_end', 'tool_execution_end', 'extension_error'):
                            print(json.dumps({'event': kind, 'status': state['status'],
                                              'tool_calls': state['tool_calls'], 'is_error': event.get('isError')}), flush=True)
                if goal and (state['status'] == 'idle' or time.monotonic() >= next_goal_poll):
                    goal.poll(state, send)
                    # Controller IPC can be slower than native output. Allow a
                    # drain interval after each poll; final turns reconcile now.
                    next_goal_poll = time.monotonic() + 1
                    if state['status'] in ('experiment_closed','experiment_stopping'): return
                save_state()
            state['status'] = 'time_limit' if process.poll() is None else 'exited'
            state['returncode'] = process.poll()
        finally:
            save_state()
            state['native_shutdown'] = shutdown_native(process, selector, send)
            save_state()
            selector.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            if goal:
                reason = ('deadline' if state['status']=='time_limit' else 'operator_stop'
                          if state['status']=='stopped' else 'infrastructure_blocked')
                goal.stopped(reason)
                state['completion'] = goal.controller.state()['completion']
                save_state()


if __name__ == '__main__':
    main()
