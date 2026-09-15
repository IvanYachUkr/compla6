"""Owner profile metadata regressions and a small real native snapshot copy."""
import contextlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from compression_lab import candidate, owner, runner
from compression_lab.engine import Engine
from compression_lab.util import Error, Ledger, digest, load, lock, save
from test_engine import data


class OwnerProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='clab-owner-profile-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cpus = sorted(os.sched_getaffinity(0))[:4]
        self.agent_uid = 65534 if os.geteuid() != 65534 else 1000
        # Metadata tests do not launch a codec or probe a native namespace.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(runner, 'probe', return_value={'status': 'available'}).start()
        mock.patch.object(runner, 'fingerprint', side_effect=self.fingerprint).start()

    def fingerprint(self, *, threads=1, cpus=None):
        value = {'native_resources': {'threads': threads, 'cpus': list(cpus or self.cpus[:1])},
                 'test_runtime': 'stable-owner-metadata-fixture'}
        return {**value, 'runtime_digest': digest(value)}

    def initialize(self):
        card_path = data(self.root / 'inputs')
        card = load(card_path)
        card['limits'].update(threads=4, cpus=self.cpus)
        card['resource_profiles'] = {'schema_version': 1, 'primary': 'primary-four', 'profiles': [
            {'id': 'reference-one', 'threads': 1, 'cpus': self.cpus[:1]},
            {'id': 'primary-four', 'threads': 4, 'cpus': self.cpus}]}
        save(card_path, card)
        self.public = self.root / 'public'
        self.engine = Engine.init(self.public, card_path)
        self.private = self.root / 'owner'
        self.private.mkdir(mode=0o700)
        sentinel = self.private / 'private-card.json'
        sentinel.write_text('Not JSON: private content must not be opened by owner init.')
        owner.init(self.private, self.agent_uid, sentinel, self.public, True)
        return load(self.private / 'owner-policy.json')

    def test_owner_init_pins_the_sealed_public_primary_runtime(self):
        policy = self.initialize()
        # The public Engine independently sealed its primary-four fingerprint.
        self.assertEqual(policy['runtime_digest'], load(self.public / 'runtime.json')['runtime_digest'])
        self.assertFalse((self.private / 'private-snapshot').exists())

    def primary_policy(self):
        policy = self.initialize()
        policy.update(schema_version=2, resource_profile={
            'id': 'primary-four', 'threads': 4, 'cpus': self.cpus},
            runtime_digest=load(self.public / 'runtime.json')['runtime_digest'])
        save(self.private / 'owner-policy.json', policy)
        return policy

    def require_release_identity(self):
        # Read-only metadata check; tests never acquire the live benchmark lock.
        host = Path(runner.HOST_LOCK)
        if os.geteuid() == 0 or not host.is_file() or host.stat().st_uid != 0:
            self.skipTest('Release owner boundary requires a non-root UID and an admin-provisioned host lock')

    def test_release_boundary_accepts_the_pinned_primary_profile(self):
        self.require_release_identity()
        policy = self.primary_policy()
        try:
            proof = owner.boundary(self.private)
        except Error as error:
            self.fail('Matching primary runtime was rejected: ' + error.code)
        self.assertEqual(proof['policy']['runtime_digest'], policy['runtime_digest'])

    def test_historical_policy_retains_default_one_thread_semantics(self):
        self.require_release_identity()
        policy = self.initialize()
        policy.update(schema_version=1, runtime_digest=self.fingerprint()['runtime_digest'])
        policy.pop('resource_profile', None)
        save(self.private / 'owner-policy.json', policy)
        self.assertEqual(owner.boundary(self.private)['policy']['schema_version'], 1)
        # Historical disclosure remains readable even after runtime drift.
        with mock.patch.object(runner, 'fingerprint', side_effect=AssertionError('Offline read must not probe runtime')):
            self.assertEqual(owner.boundary(self.private, False)['policy'], policy)

    def prepare_private_metadata(self):
        policy = self.primary_policy()
        self.ledger = Ledger(self.private / 'state')
        self.ledger.transition('public_ready', candidate_digest='a' * 64,
                               runtime_digest=policy['runtime_digest'])
        self.ledger.transition('frozen', training_object_sha256=[], public_snapshot='unused')
        save(self.private / 'frozen-card.json', load(self.public / 'dataset-card.json'))
        return policy

    def private_evaluate_metadata(self, policy, measured=None):
        # Exercise the durable owner transition/result code without reading any
        # payload, compiling a codec, or taking the campaign's host lock.
        with mock.patch.object(owner, 'boundary', return_value={'policy': policy}), \
             mock.patch.object(owner, 'lock', side_effect=lambda *a, **k: contextlib.nullcontext()), \
             mock.patch.object(owner.bridge, 'verify_candidate'), \
             mock.patch.object(owner, 'private_rows', return_value=[]) as private_rows, \
             mock.patch.object(owner.bridge, 'evaluate', side_effect=measured,
                               return_value={'status': 'eligible', 'quality_passed': True, 'eligible': True}):
            reply = owner.evaluate(self.private)
            result = load(self.private / 'results' / (reply['metrics']['owner_result_id'] + '.json'))
        return reply, result, private_rows.call_count

    def test_private_completion_keeps_the_primary_runtime_identity(self):
        policy = self.prepare_private_metadata()
        reply, result, _ = self.private_evaluate_metadata(policy)
        self.assertEqual(reply['status'], 'complete', result)
        self.assertTrue(result['quality_passed'])
        self.assertEqual(result['runtime_digest'], load(self.public / 'runtime.json')['runtime_digest'])
        self.assertEqual(self.ledger.read()['private_attempts'], 1)

    def test_changed_primary_card_is_rejected_before_private_payload_access(self):
        policy = self.prepare_private_metadata()
        card = load(self.private / 'frozen-card.json')
        card['resource_profiles']['primary'] = 'reference-one'
        card['limits'].update(threads=1, cpus=self.cpus[:1])
        save(self.private / 'frozen-card.json', card)
        reply, result, reads = self.private_evaluate_metadata(policy)
        self.assertEqual(reply['status'], 'failed')
        self.assertEqual(result['reason_codes'], ['owner_resource_profile_changed'])
        self.assertEqual(reads, 0)

    def test_runtime_change_after_private_measurement_still_fails(self):
        policy = self.prepare_private_metadata()
        with mock.patch.object(runner, 'fingerprint', return_value={'runtime_digest': 'changed-runtime'}):
            reply, result, _ = self.private_evaluate_metadata(policy)
        self.assertEqual(reply['status'], 'failed')
        self.assertEqual(result['reason_codes'], ['runtime_changed_during_private_evaluation'])

    def test_changing_only_the_pinned_cpu_set_is_runtime_drift(self):
        self.require_release_identity()
        if len(self.cpus) < 2:
            self.skipTest('The CPU-set drift case needs two available logical CPUs')
        policy = self.primary_policy()
        policy['resource_profile']['cpus'] = self.cpus[:1]
        save(self.private / 'owner-policy.json', policy)
        with self.assertRaises(Error) as caught:
            owner.boundary(self.private)
        self.assertEqual(caught.exception.code, 'owner_runtime_changed')


class OwnerCandidateCopy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The tiny native fixture and executable checks serialize with live
        # benchmarks; this class does not run a full codec gate or timing suite.
        cls.resources = contextlib.ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.resources.enter_context(lock(runner.HOST_LOCK, host=True))
        cls.root = Path(cls.resources.enter_context(tempfile.TemporaryDirectory(prefix='clab-owner-copy-')))
        from test_decoder_accounting_v2 import source_fixture
        cls.source = cls.root / 'source'
        source_fixture(cls.source)
        cls.work = cls.root / 'work'
        cls.work.mkdir()
        cls.record = candidate.register(cls.work, cls.source)
        cls.cid = cls.record['candidate_digest']
        cls.registered = cls.work / 'candidates' / cls.cid

    def test_schema_v2_owner_copy_retains_both_executables_and_decoder_bundle(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            copied = Path(directory) / self.cid
            owner.copy_candidate(self.registered, copied, self.cid)
            manifest, record = candidate.verify(copied)
            self.assertEqual(record['candidate_digest'], self.cid)
            self.assertEqual({f['path'] for f in record['decoder_runtime_files']}, {
                'decoder', 'decoder.dict', 'decoder.cfg', 'lib/libz.so.1', 'decoder-command.json'})
            self.assertEqual((copied / 'decoder-runtime/decoder.cfg').read_bytes(), b'decoder.cfg-fixture-data\n')
            self.assertFalse((copied / 'decoder-runtime/encoder.tbl').exists())
            for relative in ['runtime/encoder', 'runtime/decoder', 'decoder-runtime/decoder']:
                self.assertEqual(stat.S_IMODE((copied / relative).stat().st_mode), 0o555)
            sample = b'owner-copy-byte-exact\x00\xff\n'
            for command in [
                [str(copied / 'runtime/encoder')],
                [str(copied / 'decoder-runtime/decoder'), str(copied / 'decoder-runtime/decoder.dict'),
                 str(copied / 'decoder-runtime/decoder.cfg')],
            ]:
                decoded = subprocess.run(command, input=sample, capture_output=True, timeout=3, check=True)
                self.assertEqual(decoded.stdout, sample)
            self.assertEqual(manifest['schema_version'], 2)

    def test_decoder_runtime_symlink_is_rejected_even_when_contents_match(self):
        self.assert_link_rejected('symlink')

    def test_decoder_runtime_hardlink_is_rejected_even_when_contents_match(self):
        self.assert_link_rejected('hardlink')

    def assert_link_rejected(self, kind):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            src = root / 'source-copy' / self.cid
            shutil.copytree(self.registered, src)
            linked = src / 'decoder-runtime/decoder.cfg'
            outside = root / 'same-bytes.cfg'
            outside.write_bytes(linked.read_bytes())
            linked.unlink()
            if kind == 'symlink':
                linked.symlink_to(outside)
            else:
                os.link(outside, linked)
            dst = root / 'owner-copy' / self.cid
            with self.assertRaises((Error, OSError)):
                owner.copy_candidate(src, dst, self.cid)
            self.assertFalse((dst / 'decoder-runtime/decoder.cfg').exists())


if __name__ == '__main__':
    unittest.main()
