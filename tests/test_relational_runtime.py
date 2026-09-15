import json
import os
import tempfile
import unittest
from pathlib import Path

from compression_lab.util import Error
from compression_lab.workloads import registry
from compression_lab.workloads.session import Session


GOOD = '''import sys,json\nfor line in sys.stdin.buffer:\n r=json.loads(line); x={'version':1,'id':r['id'],'ok':True,'result':{'echo':r['op']}}\n print(json.dumps(x),flush=True)\n if r['op']=='close':break\n'''


class MutableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.root = Path(self.t.name)

    def tearDown(self):
        self.t.cleanup()

    def register(self, script=GOOD):
        p = self.root / ('source-' + str(len(list(self.root.glob('source-*')))))
        registry.create_python_candidate(p, script, 'protocol-test')
        r = registry.register(self.root, p)
        return self.root / 'candidates' / r['candidate_digest'], r

    def test_persistent_echo_and_complete_runtime_cost(self):
        p, record = self.register()
        m, r = registry.verify(p)
        costs = registry.costs(m, r)
        self.assertGreater(costs['language_runtime_bytes'], 1_000_000)
        self.assertGreater(costs['raw_source_config_bytes'], 0)
        self.assertEqual(costs['binary_bytes'], 0)
        with Session(p, self.root / 'store') as process:
            a = process.request('hello', {})
            self.assertEqual(a['result']['echo'], 'hello')
            host_pid = process.pid
            b = process.request('stats', {})
            self.assertEqual(b['result']['echo'], 'stats')
            self.assertEqual(process.pid, host_pid)
            self.assertGreater(process.operations[-1]['latency_ns'], 0)
        self.assertEqual(process.summary()['returncode'], 0)

    def test_exact_registration_key_and_tamper_failure(self):
        p, r = self.register()
        again = registry.register(self.root, self.root / 'source-0')
        self.assertEqual(r['candidate_digest'], again['candidate_digest'])
        file = p / 'runtime' / 'program.py'
        file.chmod(0o600)
        file.write_bytes(file.read_bytes() + b'# changed\n')
        with self.assertRaises(Error):
            registry.verify(p)

    def test_wrong_response_id_rejected(self):
        p, _ = self.register(GOOD.replace("'id':r['id']", "'id':r['id']+1"))
        with Session(p, self.root / 'store') as process:
            with self.assertRaisesRegex(Error, 'response_id'):
                process.request('hello', {})

    def test_oversized_unterminated_reply_is_bounded(self):
        p, _ = self.register("import sys\nsys.stdin.buffer.readline()\nsys.stdout.buffer.write(b'x'*(9*1024*1024));sys.stdout.buffer.flush()\n")
        with Session(p, self.root / 'store') as process:
            with self.assertRaisesRegex(Error, 'response_limit'):
                process.request('hello', {})

    def test_timeout_kills_process_tree(self):
        p, _ = self.register("import time\ntime.sleep(60)\n")
        process = Session(p, self.root / 'store', timeout=.3)
        with self.assertRaises(Error):
            process.request('hello', {})
        process.kill('test_cleanup')
        self.assertIsNotNone(process.summary()['returncode'])

    def test_host_files_network_and_fork_denied(self):
        canary = self.root / 'not-mounted'
        canary.write_text('SYNTHETIC PRIVATE CANARY')
        script = '''import sys,json,os,socket
r=json.loads(sys.stdin.buffer.readline()); checks={}
for key, fn in [('file',lambda:open(CANARY).read()),('network',lambda:socket.socket()),('fork',lambda:os.fork()), ('tmp',lambda:open('/tmp/hidden','wb')), ('root',lambda:open('/hidden','wb')), ('input',lambda:open('/input/hidden','wb'))]:
 try:fn();checks[key]=False
 except OSError:checks[key]=True
print(json.dumps({'version':1,'id':r['id'],'ok':True,'result':checks}),flush=True)
for line in sys.stdin.buffer:
 r=json.loads(line);print(json.dumps({'version':1,'id':r['id'],'ok':True,'result':{}}),flush=True)
 if r['op']=='close':break
'''.replace('CANARY', repr(str(canary)))
        p, _ = self.register(script)
        with Session(p, self.root / 'store') as process:
            result = process.request('hello', {})['result']
            self.assertEqual(result, {'file': True, 'network': True, 'fork': True, 'tmp': True, 'root': True, 'input': True})

    def test_bad_store_file_cannot_prevent_process_cleanup(self):
        import signal
        p, _ = self.register("import os,sys,time\nsys.stdin.buffer.readline()\nos.symlink('/candidate/program.py','escape')\ntime.sleep(60)\n")
        process = Session(p, self.root / 'store', timeout=.5)
        try:
            with self.assertRaises(Error):
                process.request('hello', {})
            self.assertTrue(process._closed)
            self.assertIsNotNone(process.summary()['returncode'])
        finally:
            if not process._closed:
                try:os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:pass
                process._wait()
                process._cleanup()

    def test_mutable_registration_refuses_exploratory_mode(self):
        p = self.root / 'exploratory-source'
        registry.create_python_candidate(p, GOOD, 'protocol-test')
        with self.assertRaises(Error) as ex:
            registry.register(self.root, p, mode='exploratory')
        self.assertEqual(ex.exception.code, 'mutable_isolation_required')

    def test_unsupported_power_loss_claim_rejected(self):
        p = self.root / 'badclaim'
        registry.create_python_candidate(p, GOOD, 'badclaim')
        m = json.loads((p / 'store_candidate.json').read_text())
        m['durability_claim'] = 'power_loss_certified'
        (p / 'store_candidate.json').write_text(json.dumps(m))
        with self.assertRaises(Error) as caught:
            registry.register(self.root, p)
        self.assertEqual(caught.exception.code, 'unsupported_durability_claim')


if __name__ == '__main__':
    unittest.main()
