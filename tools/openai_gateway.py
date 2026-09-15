"""Private, metered transport for native Prime sessions; no research logic.

Only the host process reads the real OpenAI key. Prime receives a local token.
Upstream inference has no timeout. Capacity retries are owned by this harness.
Immediate SSE headers and keepalives
prevent the local SDK's header/body timers from imposing an inference deadline.
"""
import argparse
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import threading
import time
import tempfile
import uuid

from openai_budget import Budget, CAPACITY_CODES, CONTEXT, MAX_OUTPUT, Stopped, atomic

COUNT_FIELDS = {'input', 'instructions', 'model', 'parallel_tool_calls', 'personality',
                'reasoning', 'text', 'tool_choice', 'tools', 'truncation'}
BODY_FIELDS = COUNT_FIELDS | {'stream', 'store', 'include', 'prompt_cache_key',
    'prompt_cache_retention', 'prompt_cache_options', 'max_output_tokens',
    'service_tier', 'metadata', 'temperature', 'top_p'}
MIN_FREE = 16 * 1024**3
CAPACITY_DELAYS = (15, 30, 60, 120, 180, 300)


class CapacityError(Stopped):
    pass


def capacity_code(error):
    if not isinstance(error, dict): return None
    code = error.get('code')
    if code in CAPACITY_CODES: return code
    if code is None and error.get('type') == 'service_unavailable_error': return 'service_unavailable_error'
    return None


def terminal_metadata(response):
    """Preserve billing evidence without response content or echoed error text."""
    metadata = {key: response[key] for key in
                ('id', 'model', 'status', 'service_tier', 'created_at') if key in response}
    usage = response.get('usage')
    if isinstance(usage, dict):
        metadata['usage'] = {key: usage[key] for key in
                             ('input_tokens', 'output_tokens', 'total_tokens') if key in usage}
        for key, fields in (
                ('input_tokens_details', ('cached_tokens', 'cache_write_tokens')),
                ('output_tokens_details', ('reasoning_tokens',))):
            if isinstance(usage.get(key), dict):
                metadata['usage'][key] = {field: usage[key][field] for field in fields
                                          if field in usage[key]}
    else:
        metadata['usage'] = None
        metadata['usage_type'] = type(usage).__name__
    error = response.get('error')
    if isinstance(error, dict):
        metadata['error'] = {key: error[key] for key in ('code', 'type') if key in error}
    return metadata


def credential(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise Stopped('unsafe_credential_file')
        values = {}
        for line in f:
            if '=' in line and not line.lstrip().startswith('#'):
                name, value = line.strip().split('=', 1)
                values[name.strip()] = value.strip().strip('\"\'')
    key = values.get('OPENAI_API_KEY', '')
    if not key.startswith('sk-proj-') or any(c.isspace() for c in key) or values.get('OPENAI_BASE_URL'):
        raise Stopped('invalid_credential_configuration')
    return key


def validate(body, control, session):
    if not control.get('enabled'):
        raise Stopped('campaign_not_enabled')
    if set(body) - BODY_FIELDS:
        raise Stopped('unsupported_request_fields')
    model = body.get('model')
    if model not in control['models']:
        raise Stopped('model_not_commissioned')
    if body.get('stream') is not True:
        raise Stopped('streaming_required')
    if body.get('service_tier', 'default') not in ('default', None):
        raise Stopped('nonstandard_service_tier')
    if body.get('reasoning', {}).get('mode', 'standard') != 'standard':
        raise Stopped('nonstandard_reasoning_mode')
    if any(t.get('type') != 'function' for t in body.get('tools', [])):
        raise Stopped('unmetered_hosted_tool')
    if session and model == control['parent_model'] and session != control['root_session']:
        raise Stopped('unexpected_parent_session')
    requested = body.get('max_output_tokens', MAX_OUTPUT)
    if type(requested) is not int or not 0 < requested <= MAX_OUTPUT:
        raise Stopped('invalid_output_bound')
    body.update(service_tier='default', store=False, max_output_tokens=requested)
    body['reasoning'] = {**body.get('reasoning', {}), 'effort': 'max'}
    body.pop('prompt_cache_retention', None)
    body['prompt_cache_options'] = {'mode': 'implicit', 'ttl': '30m'}
    return body


class Gateway:
    def __init__(self, control_path, ledger, key_file, evidence):
        self.control_path = Path(control_path)
        self.budget = Budget(ledger)
        initial = self.budget.snapshot()  # Never silently reset missing state.
        if any(r['status'] == 'pending' for r in initial['requests']):
            self.budget.stop('gateway_started_with_unresolved_request')
        self.key = credential(key_file)
        self.evidence = Path(evidence)
        self.lock = threading.Lock()
        self.active = set()

    def control(self):
        return json.loads(self.control_path.read_text())

    def connect(self, endpoint, body):
        connection = http.client.HTTPSConnection('api.openai.com', timeout=None)
        payload = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        try:
            connection.request('POST', '/v1/' + endpoint, body=payload, headers={
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                'Accept': 'text/event-stream' if endpoint == 'responses' else 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                status = response.status
                if status == 503:
                    try: code = capacity_code(json.loads(response.read(65537)).get('error'))
                    except (ValueError, AttributeError): code = None
                    if code: raise CapacityError(code)
                connection.close()
                raise Stopped('upstream_' + endpoint.replace('/', '_') + '_HTTP_' + str(status))
            return connection, response
        except BaseException:
            connection.close()
            raise

    def allowed(self, control):
        fresh = self.control()
        if not fresh.get('enabled'): raise Stopped('campaign_not_enabled')
        if any(fresh.get(k) != control.get(k) for k in ('active_run','root_session')):
            raise Stopped('campaign_changed_during_retry')
        deadline = fresh.get('deadline_epoch')
        if deadline is not None and time.time() >= deadline: raise Stopped('deadline_reached')
        state = self.budget.snapshot()
        if state['stopped']: raise Stopped(state['stopped'])
        if shutil.disk_usage(self.evidence).free < MIN_FREE: raise Stopped('disk_reserve_reached')

    def wait_retry(self, delay, control):
        until = time.monotonic()+delay
        while True:
            self.allowed(control)
            remaining = until-time.monotonic()
            if remaining <= 0: return
            time.sleep(min(1, remaining))

    def receive_attempt(self, response, ticket, output):
        pending = b''
        while True:
            line = response.readline()
            if not line: raise Stopped('provider_stream_ended_without_usage')
            pending += line
            if len(pending) > 32*1024**2: raise Stopped('oversize_provider_event')
            if line.strip(): continue
            terminal = False
            for item in pending.splitlines():
                if not item.startswith(b'data: ') or item == b'data: [DONE]': continue
                event = json.loads(item[6:])
                if event.get('type') == 'error':
                    code = capacity_code(event.get('error', event))
                    if code: raise CapacityError(code)
                    raise Stopped('provider_error_event')
                if event.get('type') not in ('response.completed','response.incomplete','response.failed'): continue
                obj = event['response']
                atomic(self.evidence/f'terminal-{ticket:05d}.json', {
                    'ticket':ticket,'event_type':event['type'],'received_epoch':time.time(),
                    'response':terminal_metadata(obj)})
                code = capacity_code(obj.get('error')) if event['type']=='response.failed' else None
                if code:
                    requested = self.budget.snapshot()['requests'][ticket-1]['model']
                    if (not isinstance(obj.get('model'), str) or not (obj['model']==requested or obj['model'].startswith(requested+'-'))
                            or obj.get('service_tier') not in (None,'default')):
                        raise Stopped('capacity_response_identity_mismatch')
                    if not isinstance(obj.get('usage'), dict): raise CapacityError(code)
                if not isinstance(obj.get('usage'), dict): raise Stopped('provider_response_failed_without_usage')
                self.budget.settle(ticket,obj['usage'],response_id=obj['id'],
                    response_model=obj['model'],service_tier=obj.get('service_tier'))
                if code: raise CapacityError(code)
                if event['type']=='response.failed': self.budget.stop('provider_response_failed')
                terminal = True
            output.write(pending)
            pending = b''
            if terminal: return

    def upstream(self, body, control, session, emit):
        ticket = None
        connection = None
        logical_id = uuid.uuid4().hex
        count = None
        try:
            for attempt in range(len(CAPACITY_DELAYS)+1):
                ticket = None
                try:
                    self.allowed(control)
                    if count is None:
                        connection, response = self.connect('responses/input_tokens',
                            {k:v for k,v in body.items() if k in COUNT_FIELDS})
                        count = json.loads(response.read()).get('input_tokens')
                        connection.close(); connection = None
                        if type(count) is not int or not 0 < count <= CONTEXT: raise Stopped('invalid_input_token_count')
                    input_bound = min(CONTEXT,count+4096)
                    payload_sha = hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
                    ticket = self.budget.reserve(control['active_run'],session or 'auxiliary-'+body['model'],
                        body['model'],input_bound,body['max_output_tokens'],payload_sha)
                    atomic(self.evidence/f'request-{ticket:05d}.json', {
                        'ticket':ticket,'logical_request':logical_id,'attempt':attempt+1,
                        'run':control['active_run'],'model':body['model'],'reasoning_effort':body['reasoning']['effort'],
                        'counted_input_tokens':count,'input_bound':input_bound,'max_output_tokens':body['max_output_tokens'],
                        'service_tier':body['service_tier'],'payload_sha256':payload_sha,
                        'client_retries':0,'capacity_retry_delays':list(CAPACITY_DELAYS),
                        'upstream_timeout_seconds':None,'started_epoch':time.time()})
                    connection, response = self.connect('responses',body)
                    # Buffer each attempt until settlement so a failed attempt's
                    # partial tool calls and response IDs cannot leak into its retry.
                    with tempfile.SpooledTemporaryFile(max_size=1024**2, dir=self.evidence) as output:
                        self.receive_attempt(response,ticket,output)
                        output.seek(0)
                        while chunk := output.read(65536): emit(chunk)
                    break
                except CapacityError as exc:
                    code = str(exc)
                    if ticket is not None: self.budget.capacity_failure(ticket,code)
                    delay = CAPACITY_DELAYS[attempt] if attempt<len(CAPACITY_DELAYS) else None
                    atomic(self.evidence/f'retry-{logical_id}-{attempt+1}.json',{
                        'logical_request':logical_id,'attempt':attempt+1,'ticket':ticket,
                        'capacity_code':code,'next_delay_seconds':delay,'received_epoch':time.time()})
                    if connection is not None: connection.close(); connection = None
                    if delay is None: raise Stopped('capacity_retries_exhausted')
                    if ticket is not None:
                        snapshot = self.budget.snapshot()
                        if snapshot['remaining_nano'] < snapshot['requests'][ticket-1]['reserved_nano']:
                            raise Stopped('budget_cannot_cover_next_request')
                    self.wait_retry(delay,control)
                finally:
                    if connection is not None: connection.close(); connection = None
        except Exception as exc:
            reason = str(exc) if isinstance(exc, Stopped) else type(exc).__name__
            if ticket is None:
                self.budget.stop(reason)
            else:
                self.budget.uncertain(ticket, reason)
            emit(('event: error\ndata: ' + json.dumps({'type': 'error', 'code': 'campaign_stopped',
                  'message': reason}) + '\n\n').encode())
        finally:
            if connection is not None: connection.close()
            emit(None)


def handler_for(gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def error(self, status, code):
            data = json.dumps({'error': {'type': 'campaign_guard', 'code': code, 'message': code}}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.send_header('x-should-retry', 'false')
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def do_POST(self):
            control = gateway.control()
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied, 'Bearer ' + control['local_token']):
                return self.error(401, 'unauthorized')
            if self.path != '/v1/responses':
                gateway.budget.stop('uncommissioned_api_endpoint')
                return self.error(403, 'uncommissioned_api_endpoint')
            session = None
            admitted = False
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32 * 1024**2:
                    raise Stopped('invalid_request_size')
                body = json.loads(self.rfile.read(size))
                session = body.get('prompt_cache_key') or self.headers.get('x-client-request-id')
                body = validate(body, control, session)
                admission_key = object()
                with gateway.lock:
                    if len(gateway.active) >= control['maximum_inflight']:
                        raise Stopped('unexpected_concurrent_request')
                    if session and body['model'] != control['parent_model']:
                        fresh = gateway.control()
                        if fresh.get('child_session') not in (None, session):
                            raise Stopped('unexpected_additional_child_session')
                        if fresh.get('child_session') is None:
                            fresh['child_session'] = session
                            atomic(gateway.control_path, fresh)
                    gateway.active.add(admission_key)
                    admitted = True
                state = gateway.budget.snapshot()
                if state['stopped']:
                    raise Stopped(state['stopped'])
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b': accepted\n\n')
                self.wfile.flush()
                self.close_connection = True
                chunks = queue.Queue(maxsize=32)
                disconnected = threading.Event()

                def emit(chunk):
                    while not disconnected.is_set():
                        try:
                            chunks.put(chunk, timeout=1)
                            return
                        except queue.Full:
                            continue

                worker = threading.Thread(target=gateway.upstream,
                    args=(body, control, session, emit), daemon=True)
                worker.start()
                try:
                    while True:
                        try:
                            chunk = chunks.get(timeout=10)
                        except queue.Empty:
                            chunk = b': waiting for provider\n\n'
                        if chunk is None:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    disconnected.set()
                    gateway.budget.stop('native_client_disconnected')
                finally:
                    # Reconcile an already-paid request even if Prime disappears.
                    disconnected.set()
                    worker.join()
            except Exception as exc:
                reason = str(exc) if isinstance(exc, Stopped) else type(exc).__name__
                gateway.budget.stop(reason)
                if not self.close_connection:
                    self.error(409, reason)
            finally:
                if admitted:
                    with gateway.lock:
                        gateway.active.discard(admission_key)
    return Handler


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--control', required=True)
    p.add_argument('--ledger', required=True)
    p.add_argument('--key-file', required=True)
    p.add_argument('--evidence', required=True)
    p.add_argument('--port', type=int, default=8896)
    a = p.parse_args()
    gateway = Gateway(a.control, a.ledger, a.key_file, a.evidence)
    server = ThreadingHTTPServer(('127.0.0.1', a.port), handler_for(gateway))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__':
    main()
