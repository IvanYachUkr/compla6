import tempfile
import time
import unittest
from pathlib import Path

from compression_lab import baselines, candidate
from compression_lab.engine import Engine
from compression_lab.util import Error, digest, save
from test_engine import data


class EvidenceProvenance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.engine = Engine.init(self.path / 'work', data(self.path / 'data'))

    def finish(self, name, cost):
        e = self.engine
        job = {'job_id': name, 'run_id': e.state()['run_id'],
               'candidate_digest': name, 'track': 'agent', 'depth': 'full',
               'status': 'running', 'created_epoch': time.time()}
        def reserve(state):
            state['active_job'] = name
            state['budgets'].setdefault('agent', {'evaluations': 0, 'wall_seconds': 0})['evaluations'] += 1
            return state
        e.ledger.update('test_reserve', reserve)
        save(e.root / 'jobs' / name / 'job.json', job)
        raw = {'status': 'eligible', 'eligible': True, 'quality_passed': True,
               'wall_seconds': 1, 'depth': 'full',
               'accounting': {'development': {'projection': {'deployment_total_bytes': cost}}}}
        return job, raw, e._finish_job(dict(job), raw)

    def test_digest_valid_uncommitted_result_is_rejected(self):
        e = self.engine
        state = e.state()
        raw = {'status': 'eligible', 'eligible': True, 'run_id': state['run_id'],
               'card_digest': state['card_digest'], 'runtime_digest': state['runtime_digest']}
        result = 'r-' + digest(raw)
        save(e.root / 'results' / result / 'raw.json', raw)
        with self.assertRaises(Error) as caught:
            e.raw(result)
        self.assertEqual(caught.exception.code, 'result_not_committed')

    def test_unregistered_snapshot_cannot_start_scored_job(self):
        e = self.engine
        baselines.create(self.path / 'source', 'stored')
        registration = candidate.register(e.root, self.path / 'source')
        try:
            with self.assertRaises(Error) as caught:
                e.evaluate(registration['candidate_digest'], 'quick')
            self.assertEqual(caught.exception.code, 'candidate_not_registered')
            self.assertEqual(e.state()['budgets'], {})
        finally:
            job = e.state()['active_job']
            if job:
                e.cancel(job)
                e.wait(job, 30)

    def test_best_result_survives_later_worse_eligible_result(self):
        self.finish('best', 100)
        self.finish('worse', 120)
        self.assertEqual(self.engine.state()['best_eligible_candidate'], 'best')

    def test_finishing_same_job_twice_preserves_committed_result(self):
        job, raw, first = self.finish('once', 100)
        second = self.engine._finish_job(dict(job), raw)
        self.assertEqual(second, first)
        self.assertEqual(self.engine._job('once')['result_id'], first)
        self.assertEqual(self.engine.state()['results'], [first])
        self.assertEqual(self.engine.state()['budgets']['agent']['wall_seconds'], 1)
