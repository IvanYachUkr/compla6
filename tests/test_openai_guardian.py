"""Retained capacity charges and normal completion do not become false faults."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from openai_budget import Budget, atomic
import openai_guardian as guardian


class EndTick(BaseException): pass


class GuardianTests(unittest.TestCase):
    def test_failed_native_state_probe_closes_admission_before_services_stop(self):
        self.assert_native_fault_stops('native_state_error')

    def test_rejected_prompt_closes_admission_before_services_stop(self):
        self.assert_native_fault_stops('native_prompt_error')

    def assert_native_fault_stops(self, native_status):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); control=base/'control'; control.mkdir()
            budget=Budget(control/'budget.json'); budget.create()
            campaign=base/'campaign.json'; output=base/'driver'; output.mkdir()
            atomic(campaign,{'driver_unit':'fixture-driver','evaluator_unit':'fixture-evaluator',
                'driver_output':str(output),'maximum_children':0})
            atomic(control/'state.json',{'active_run':'run','status':'running','runs':{'run':{'campaign':str(campaign)}}})
            atomic(control/'gateway.json',{'enabled':True})
            atomic(output/'status.json',{'status':native_status,'completed_prompts':0})
            def stopped(*args, **kwargs):
                self.assertFalse(json.loads((control/'gateway.json').read_text())['enabled'])
                self.assertEqual(budget.snapshot()['stopped'],'native_'+native_status)
            with patch.object(sys,'argv',['guardian','--base',str(base)]), \
                 patch.object(guardian,'unit_state',return_value={'ActiveState':'active'}), \
                 patch.object(guardian.shutil,'disk_usage',return_value=type('Disk',(),{'free':32*1024**3})()), \
                 patch.object(guardian.time,'sleep',side_effect=EndTick), \
                 patch.object(guardian.subprocess,'run',side_effect=stopped) as services:
                with self.assertRaises(EndTick): guardian.main()
            services.assert_called_once_with(['systemctl','stop','fixture-driver','fixture-evaluator'],check=True)
            self.assertFalse((control/'completion-run.json').exists())

    def test_capacity_hold_and_completed_run_are_distinguished_from_infrastructure_failure(self):
        for completed in (False, True):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as temp:
                base=Path(temp); control=base/'control'; control.mkdir()
                budget=Budget(control/'budget.json'); budget.create()
                ticket=budget.reserve('run','native','gpt-5.6-sol',100,100,'sha')
                budget.capacity_failure(ticket,'server_is_overloaded')
                campaign=base/'campaign.json'; output=base/'driver'; output.mkdir()
                atomic(campaign,{'driver_unit':'fixture-driver','evaluator_unit':'fixture-evaluator',
                    'driver_output':str(output),'maximum_children':0})
                atomic(control/'state.json',{'active_run':'run','status':'running','runs':{'run':{'campaign':str(campaign)}}})
                atomic(control/'gateway.json',{'enabled':True})
                atomic(output/'status.json',{'status':'experiment_closed' if completed else 'running',
                    'completion':{'accepted':True,'stop_reason':'attempt_limit'}})
                with patch.object(sys,'argv',['guardian','--base',str(base)]), \
                     patch.object(guardian,'unit_state',return_value={'ActiveState':'inactive' if completed else 'active'}), \
                     patch.object(guardian.shutil,'disk_usage',return_value=type('Disk',(),{'free':32*1024**3})()), \
                     patch.object(guardian.time,'sleep',side_effect=EndTick), \
                     patch.object(guardian.subprocess,'run') as services:
                    with self.assertRaises(EndTick): guardian.main()
                services.assert_not_called()
                self.assertIsNone(budget.snapshot()['stopped'])
                self.assertEqual(json.loads((control/'gateway.json').read_text())['enabled'],not completed)
                self.assertEqual((control/'completion-run.json').exists(),completed)
                self.assertFalse((control/'stop-run.json').exists())


if __name__=='__main__':unittest.main()
