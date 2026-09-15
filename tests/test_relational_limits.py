import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab.util import Error
from compression_lab.workloads import protocol, registry, relational
from compression_lab.workloads.fixtures import movie_schema
from compression_lab.workloads.oracle import ReferenceState
from compression_lab.workloads.session import Session


def fanout_seed():
    schema = json.dumps(movie_schema()).encode()
    return relational.pack(schema, [
        ('titles', b'7,tt0000007,title,2026,title\n'),
        ('entities', b'3,' + b'a' * 500_000 + b'\n'),
        ('episodes', b''),
        ('relationships', b''.join(f'{i},7,3,actor\n'.encode() for i in range(20)))])


class RelationalLimits(unittest.TestCase):
    def test_fd_permission_race_is_ignored_only_for_exiting_processes(self):
        from compression_lab import runner
        with tempfile.TemporaryDirectory() as folder:
            session = Session.__new__(Session)
            session.store, session.pid = Path(folder), 123
            with patch.object(runner, 'process_tree', return_value={123}), \
                 patch.object(Path, 'iterdir', side_effect=PermissionError), \
                 patch.object(Path, 'read_text') as read:
                for status, flags in [('Z', 0), ('X', 0), ('R', 4)]:
                    read.return_value = f'123 (worker) {status} 1 1 1 0 -1 {flags}'
                    self.assertEqual(session._unlinked_space(), (0, 0))
                read.return_value = '123 (worker) R 1 1 1 0 -1 0'
                with self.assertRaises(Error) as caught:
                    session._unlinked_space()
                self.assertEqual(caught.exception.code, 'mutable_fd_inventory_unavailable')

    def test_oracle_bounds_high_fanout_query_before_serializing(self):
        with self.assertRaises(Error) as caught:
            ReferenceState(fanout_seed()).join('title_entities', 7)
        self.assertEqual(caught.exception.code, 'oracle_query_output_limit')

    def test_reference_returns_query_limit_error_and_remains_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry.create_reference(root / 'source')
            reg = registry.register(root, root / 'source')
            candidate = root / 'candidates' / reg['candidate_digest']
            session = Session(candidate, root / 'store')
            try:
                self.assertTrue(session.request('hello', {})['ok'])
                seed = fanout_seed()
                self.assertTrue(session.request('build', {'bundle_b64': base64.b64encode(seed).decode()})['ok'])
                response = session.request('join', {'kind': 'title_entities', 'id': 7})
                self.assertFalse(response['ok'])
                self.assertEqual(response['error']['code'], 'query_output_limit')
                exported = session.request('export', {})
                self.assertEqual(protocol.decode_blob(exported['result']['bytes_b64']), seed)
            finally:
                if not session._closed:
                    session.kill('test_cleanup')

    def test_unlinked_open_files_count_toward_store_limit(self):
        source = '''import json,os,sys,time
handles=[]
for line in sys.stdin:
 r=json.loads(line)
 for i in range(2):
  p='/output/hidden-'+str(i)
  fd=os.open(p,os.O_CREAT|os.O_RDWR,0o600)
  os.write(fd,b'x'*(2*1024*1024))
  os.unlink(p)
  handles.append(fd)
 time.sleep(.15)
 print(json.dumps({'version':1,'id':r['id'],'ok':True,'result':{}}),flush=True)
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry.create_python_candidate(root / 'source', source, 'unlinked-space-control')
            reg = registry.register(root, root / 'source')
            session = Session(root / 'candidates' / reg['candidate_digest'], root / 'store',
                              output_limit=3*1024*1024, timeout=5)
            try:
                with self.assertRaises(Error) as caught:
                    session.request('hello', {})
                self.assertEqual(caught.exception.code, 'mutable_store_space_limit')
            finally:
                if not session._closed:
                    session.kill('test_cleanup')


if __name__ == '__main__':
    unittest.main()
