"""Real Engine ledger/transport regressions without codec execution."""
import importlib.util
import json
import tempfile
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch
from compression_lab.engine import Engine
from compression_lab.controller import Controller
from compression_lab.util import Error, save
from test_engine import data
from test_controller import protocol


class EngineControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.e=Engine.init(root/'workspace',data(root/'data'),exploratory=True)
        self.c=Controller.configure(self.e,protocol())

    def queued(self, request_id='trial-one'):
        cid='a'*64
        self.e.ledger.update('fixture_register',lambda s:{**s,'registered':[cid]})
        with patch('compression_lab.engine.bridge.verify_candidate',return_value=({'input_domain':'opaque_bytes'},{})), patch('compression_lab.engine.runner.require_fingerprint',side_effect=lambda p,**kw:p), patch('compression_lab.engine.subprocess.Popen') as spawn:
            result=self.e.evaluate(cid,request_id=request_id)
            return result,spawn.call_count

    def test_evaluation_request_replays_same_job_after_reinstantiation(self):
        result,count=self.queued()
        again=Engine(self.e.root).evaluate('a'*64,request_id='trial-one')
        self.assertEqual(again['job_id'],result['job_id'])
        self.assertEqual(count,1)
        self.assertEqual(self.e.state()['budgets']['agent']['evaluations'],1)
        with self.assertRaisesRegex(Error,'request_id_conflict'):
            self.e.evaluate('a'*64,depth='screen',request_id='trial-one')

    def test_finish_cannot_close_between_admission_and_job_dispatch(self):
        self.e.ledger.update('fixture_register',lambda s:{**s,'registered':['a'*64]})
        attempts=[]
        def verify(path):
            attempts.append(self.c.finish('during-registration',outcome='negative'))
            return {'input_domain':'opaque_bytes'},{}
        with patch('compression_lab.engine.bridge.verify_candidate',side_effect=verify), patch('compression_lab.engine.runner.require_fingerprint',side_effect=lambda p,**kw:p), patch('compression_lab.engine.subprocess.Popen'):
            self.e.evaluate('a'*64,request_id='trial')
        self.assertFalse(attempts[0]['accepted'])
        self.assertIn('operation_in_progress',attempts[0]['reason_codes'])
        self.assertEqual(self.c.state()['lifecycle'],'open')

    def test_queued_worker_crash_does_not_spend_active_compute_budget(self):
        result,_=self.queued()
        job=self.e._job(result['job_id'])
        job.update(created_epoch=time.time()-120,pid=99999999,proc_start='0')
        save(self.e.root/'jobs'/job['job_id']/'job.json',job)
        result=self.e.status(job=job['job_id'])
        self.assertEqual(self.e.state()['budgets']['agent']['wall_seconds'],0)
        self.assertGreaterEqual(result['metrics']['queue_wait_seconds'],120)
        self.assertEqual(result['metrics']['active_wall_seconds'],0)

    def test_running_worker_crash_preserves_queue_and_compute_components(self):
        result,_=self.queued()
        job=self.e._job(result['job_id'])
        job.update(created_epoch=time.time()-120,active_started_epoch=time.time()-2,pid=99999999,proc_start='0')
        save(self.e.root/'jobs'/job['job_id']/'job.json',job)
        result=self.e.status(job=job['job_id'])
        self.assertLess(self.e.state()['budgets']['agent']['wall_seconds'],5)
        self.assertGreaterEqual(result['metrics']['queue_wait_seconds'],117)
        self.assertGreaterEqual(result['metrics']['active_wall_seconds'],2)

    def test_closed_controller_blocks_factories_and_registration_before_build(self):
        self.e.finish('done',outcome='negative')
        for operation in (lambda:self.e.register('missing'),lambda:self.e.create_recipe('stored','after-finish')):
            with self.assertRaisesRegex(Error,'controller_closed'):operation()
        with patch('compression_lab.engine.baselines.create') as factory:
            with self.assertRaisesRegex(Error,'controller_closed'):self.e.control(evaluations=1)
            factory.assert_not_called()
        self.assertEqual(self.e.state()['budgets'],{})

    def test_corrupt_prior_result_cannot_block_current_cancellation_charge(self):
        from compression_lab.util import load
        first,_=self.queued('first');job=self.e._job(first['job_id'])
        rid=self.e._finish_job(job,{'status':'failed','quality_passed':False,'eligible':False,
                                  'active_wall_seconds':1,'wall_seconds':1})
        path=self.e.root/'results'/rid/'raw.json';raw=load(path);raw['wall_seconds']=2;save(path,raw)
        second,_=self.queued('second');job=self.e._job(second['job_id'])
        self.c.stop('operator_stop')
        rid2=self.e._finish_job(job,{'status':'cancelled','quality_passed':False,'eligible':False,
                                   'active_wall_seconds':3,'wall_seconds':3})
        self.assertEqual(self.e.state()['budgets']['agent']['wall_seconds'],4)
        self.assertIsNone(self.e.state()['active_job'])
        self.assertEqual(self.c.state()['lifecycle'],'closed')
        self.assertEqual(self.c.state()['completion']['evidence_error'],'stale_result_digest')
        self.assertEqual(self.e._finish_job(job,{}),rid2)
        self.assertEqual(self.e.state()['budgets']['agent']['wall_seconds'],4)

    def test_worker_persists_active_start_before_loading_data(self):
        from compression_lab.worker import run
        first,_=self.queued('active-start');job=self.e._job(first['job_id']);observed=[]
        def fail_data(engine):
            observed.append(engine._job(job['job_id']))
            raise Error('fixture_loading_failure')
        with patch('compression_lab.worker.runner.HOST_LOCK',self.e.root/'fixture-host.lock'), patch('compression_lab.worker.runner.require_fingerprint',side_effect=lambda p,**kw:p), patch.object(Engine,'data',fail_data):
            run(self.e.root,job['job_id'])
        self.assertEqual(len(observed),1)
        self.assertIsInstance(observed[0].get('active_started_epoch'),float)
        self.assertEqual(observed[0]['status'],'running')
        raw=self.e.raw(self.e.state()['result_id'])
        self.assertGreater(raw['active_wall_seconds'],0)

    def test_malformed_prior_best_does_not_block_charge_or_drain(self):
        for index,broken in enumerate(('{','[]')):
            with self.subTest(broken=broken):
                first,_=self.queued('bad-old-'+str(index));job=self.e._job(first['job_id'])
                rid=self.e._finish_job(job,{'status':'failed','quality_passed':False,'eligible':False,
                                          'active_wall_seconds':1,'wall_seconds':1})
                path=self.e.root/'results'/rid/'raw.json';path.chmod(0o600);path.write_text(broken)
                self.e.ledger.update('fixture_best',lambda s:{**s,'best_eligible_result':rid})
                second,_=self.queued('new-cancel-'+str(index));job=self.e._job(second['job_id'])
                rid2=self.e._finish_job(job,{'status':'cancelled','quality_passed':False,'eligible':False,
                                           'active_wall_seconds':3,'wall_seconds':3})
                self.assertEqual(self.e.state()['budgets']['agent']['wall_seconds'],4*(index+1))
                self.assertIsNone(self.e.state()['active_job'])
                self.assertEqual(self.e.state()['best_evidence_error']['result_id'],rid)
                self.assertEqual(self.e._finish_job(job,{}),rid2)
        self.c.stop('operator_stop')
        self.assertEqual(self.c.state()['lifecycle'],'closed')
        self.assertEqual(self.c.state()['completion']['evidence_error'],'evidence_unavailable')

    def test_stop_cancels_active_job_and_finalizes_after_drain(self):
        result,_=self.queued()
        job=self.e._job(result['job_id'])
        stopped=self.c.stop('operator_stop')
        self.assertEqual(stopped['status'],'stopping')
        self.assertEqual(self.c.state()['lifecycle'],'stopping')
        self.assertTrue((self.e.root/'jobs'/job['job_id']/'cancel.json').is_file())
        self.assertIsNone(self.c.state()['completion'])
        self.e._finish_job(job,{'status':'cancelled','quality_passed':False,'eligible':False,
                              'wall_seconds':1,'active_wall_seconds':0,'queue_wait_seconds':1})
        self.assertEqual(self.c.state()['lifecycle'],'closed')
        self.assertTrue(self.c.state()['completion']['drain_complete'])

    def test_registration_deadline_is_clamped_before_native_build(self):
        observed=[]
        def fake_register(root,path,mode,limits,*rest):
            observed.append(limits['_deadline']-time.monotonic())
            raise Error('fixture_end')
        remaining=self.c.state()['protocol']['deadline_epoch']-time.time()
        # No native operation: use a private fixture lock rather than contend with live jobs.
        with patch('compression_lab.engine.bridge.register_candidate',side_effect=fake_register), patch('compression_lab.engine.runner.HOST_LOCK',self.e.root/'fixture-host.lock'), patch('compression_lab.engine.runner.require_fingerprint',side_effect=lambda p,**kw:p):
            with self.assertRaisesRegex(Error,'fixture_end'):self.e.register('unused')
        self.assertLessEqual(observed[0],remaining)
        self.assertLess(observed[0],self.e.state()['search_budget']['wall_seconds'])

    def test_busy_registration_does_not_consume_a_candidate_trial(self):
        from compression_lab.util import lock
        host=self.e.root/'fixture-host.lock'
        with patch('compression_lab.engine.runner.HOST_LOCK',host), patch('compression_lab.engine.runner.require_fingerprint',side_effect=lambda p,**kw:p), lock(host):
            with self.assertRaisesRegex(Error,'busy'):self.e.register('unused')
        self.assertEqual(self.e.state()['budgets']['agent']['evaluations'],0)
        self.assertGreaterEqual(self.e.state()['budgets']['agent']['wall_seconds'],0)
        self.assertEqual(self.e.state()['registration_failures'][-1]['reason_code'],'busy')


@unittest.skipUnless(importlib.util.find_spec('mcp'),'Official MCP SDK unavailable')
class ControllerMCP(unittest.IsolatedAsyncioTestCase):
    async def test_public_completion_roundtrip_and_admin_tools_absent(self):
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);engine=Engine.init(root/'workspace',data(root/'data'),exploratory=True)
            Controller.configure(engine,protocol())
            code='import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]/'src'))+');from compression_lab.mcp_server import main;main()'
            params=StdioServerParameters(command=sys.executable,args=['-I','-c',code,'--workspace',str(engine.root)])
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    names={t.name for t in (await session.list_tools()).tools}
                    self.assertTrue({'experiment_brief','finish','lesson_propose','lesson_check','lesson_list'}<=names)
                    self.assertFalse({'configure','controller_host','admit','reserve','settle','lesson_rollback','freeze','evaluate_private'}&names)
                    async def call(name,args):
                        out=await session.call_tool(name,args)
                        self.assertFalse(out.isError,out)
                        return out.structuredContent or json.loads(out.content[0].text)
                    brief=await call('experiment_brief',{})
                    self.assertEqual(brief['metrics']['lifecycle'],'ready')
                    denied=await call('finish',{'request_id':'premature'})
                    self.assertEqual(denied['status'],'rejected')
                    closed=await call('finish',{'request_id':'negative','outcome':'negative'})
                    self.assertEqual(closed['status'],'finished')
                    self.assertEqual(closed,await call('finish',{'request_id':'negative','outcome':'negative'}))
                    self.assertEqual((await call('resume',{}))['metrics']['lifecycle'],'closed')


if __name__=='__main__':unittest.main()
