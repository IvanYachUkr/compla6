"""Offline activity checks for retained native child follow-ups."""
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from prime_observer import child_active, project_native_child


class NativeActivityTests(unittest.TestCase):
    def project(self, *events):
        identity = [
            {'type': 'session', 'id': 'same-child'},
            {'type': 'model_change', 'provider': 'openai-codex', 'modelId': 'gpt-5.6-luna'},
            {'type': 'thinking_level_change', 'thinkingLevel': 'high'},
        ]
        return project_native_child(identity + list(events), {'status': 'done'})

    def test_completed_child_idle(self):
        self.assertFalse(child_active(self.project({'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'stop'}})))

    def test_new_handoff_reactivates_completed_child(self):
        self.assertTrue(child_active(self.project(
            {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'stop'}},
            {'type': 'custom_message', 'customType': 'agent_message'})))

    def test_tool_work_active_even_after_initial_task_done(self):
        child = self.project({'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'toolUse', 'content': [{'type': 'toolCall'}]}})
        self.assertTrue(child_active(child))
        self.assertEqual(child['toolUseCount'], 1)

    def test_result_awaiting_next_response_is_active(self):
        self.assertTrue(child_active(self.project({'type': 'message', 'message': {'role': 'toolResult'}})))

    def test_error_does_not_establish_idle_during_retry(self):
        self.assertTrue(child_active(self.project({'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'error'}})))

    def test_missing_lifecycle_and_wrong_effort_fail_closed(self):
        self.assertTrue(child_active(self.project()))
        child = self.project({'type': 'thinking_level_change', 'thinkingLevel': 'medium'},
                             {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'stop'}})
        self.assertTrue(child_active(child))
        self.assertIn('native_observation_error', child)


if __name__ == '__main__':
    unittest.main()
