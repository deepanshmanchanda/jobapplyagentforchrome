import tempfile
import unittest
from pathlib import Path

from extension_bridge import ExtensionBridge
from model_application_planner import ModelApplicationPlanner


class Provider:
    def __init__(self, indexes=None):
        self.indexes = [0] if indexes is None else indexes
        self.calls = []
        self.callback = None

    def decide_application_actions(self, *, observation, schema):
        self.calls.append(observation)
        if self.callback:
            self.callback()
        return dict(action_indexes=self.indexes, reason='Choose the relevant current control.', needs_user_input=not self.indexes)


class ModelPlannerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.provider = Provider()
        self.bridge = ExtensionBridge(Path(self.folder.name) / 'jobs.sqlite3', decision_planner=ModelApplicationPlanner(self.provider))

    def snapshot(self):
        return dict(tabId='42', url='https://example.com/apply', documentKey='form', fields=[
            dict(selector='#first', label='First name', type='text', value='')])

    def test_all_normal_fills_get_model_decision(self):
        self.bridge.save_snapshot(self.snapshot())
        status = self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        self.assertTrue(status['modelBacked'])
        self.assertEqual(self.provider.calls[0]['actions'][0]['value'], 'Test')
        self.assertEqual(self.bridge.pending_commands('42')[0]['value'], 'Test')

    def test_model_selects_navigation_instead_of_fixed_priority(self):
        self.provider.indexes = [1]
        self.bridge.save_snapshot(dict(tabId='42', url='https://example.com', documentKey='nav', fields=[], actions=[
            dict(selector='safe-action:continue:0', label='Continue'),
            dict(selector='safe-action:apply now:0', label='Apply now')]))
        self.bridge.start_autopilot('42', {}, {})
        self.assertEqual(self.bridge.pending_commands('42')[0]['label'], 'Apply now')

    def test_refusal_is_cached_without_fallback(self):
        self.provider.indexes = []
        snapshot = self.snapshot()
        self.bridge.save_snapshot(snapshot)
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        self.bridge.save_snapshot(snapshot)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.bridge.pending_commands('42'), [])
        self.assertEqual(self.bridge.autopilot_status('42')['state'], 'needs_user_input')

    def test_invalid_model_action_never_reaches_browser_or_retries_each_heartbeat(self):
        self.provider.indexes = [99]
        snapshot = self.snapshot()
        self.bridge.save_snapshot(snapshot)
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        self.bridge.save_snapshot(snapshot)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.bridge.pending_commands('42'), [])

    def test_changed_page_discards_model_decision(self):
        snapshot = self.snapshot()
        self.bridge.save_snapshot(snapshot)
        changed = self.snapshot()
        changed['fields'][0]['value'] = 'Already entered'
        self.provider.callback = lambda: self.bridge.save_snapshot(changed)
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        self.assertEqual(self.bridge.pending_commands('42'), [])
        self.assertEqual(self.bridge.autopilot_status('42')['state'], 'waiting_for_snapshot')

    def test_no_progress_does_not_repeat_forever(self):
        snapshot = self.snapshot()
        self.bridge.save_snapshot(snapshot)
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        for _ in range(2):
            command = self.bridge.pending_commands('42')[0]
            self.bridge.add_result(dict(tabId='42', results=[dict(commandId=command['commandId'], status='applied')]))
            self.bridge.save_snapshot(snapshot)
        self.assertEqual(self.bridge.pending_commands('42'), [])
        self.assertIn('two attempts', self.bridge.autopilot_status('42')['reason'])

    def test_structural_batch_executes_only_first_model_selected_action(self):
        self.provider.indexes = [0, 1]
        planner = ModelApplicationPlanner(self.provider)
        chosen, _ = planner.choose({}, [dict(kind='click', label='Next'), dict(kind='fill', label='Name', value='Test')], {})
        self.assertEqual(chosen, [dict(kind='click', label='Next')])

    def test_transient_failure_retries_once_without_sending_browser_actions(self):
        class APITimeoutError(Exception):
            pass
        def timeout(**kwargs):
            self.provider.calls.append(kwargs)
            raise APITimeoutError('private API payload')
        self.provider.decide_application_actions = timeout
        snapshot = self.snapshot()
        self.bridge.save_snapshot(snapshot)
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        status = self.bridge.autopilot_status('42')
        self.assertEqual(status['modelFailureCode'], 'timeout')
        self.assertIn('Retrying once automatically', status['reason'])
        self.assertNotIn('private', status['reason'])
        self.assertEqual(self.bridge.pending_commands('42'), [])
        self.bridge.save_snapshot(snapshot)
        self.assertEqual(len(self.provider.calls), 1)
        self.bridge._autopilot['42']['model_retry_after'] = 0
        self.bridge.save_snapshot(snapshot)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertIn('automatic retry also failed', self.bridge.autopilot_status('42')['reason'])
        self.bridge.save_snapshot(snapshot)
        self.assertEqual(len(self.provider.calls), 2)

    def test_transient_failure_recovers_on_next_decision(self):
        class APIConnectionError(Exception):
            pass
        original = self.provider.decide_application_actions
        def fail_once(**kwargs):
            self.provider.decide_application_actions = original
            raise APIConnectionError()
        self.provider.decide_application_actions = fail_once
        snapshot = self.snapshot()
        self.bridge.save_snapshot(snapshot)
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        self.bridge._autopilot['42']['model_retry_after'] = 0
        self.bridge.save_snapshot(snapshot)
        self.assertEqual(len(self.bridge.pending_commands('42')), 1)
        self.assertEqual(self.bridge.autopilot_status('42')['modelFailureCode'], '')

    def test_authentication_failure_has_specific_non_retrying_message(self):
        class AuthenticationError(Exception):
            status_code = 401
        def fail(**kwargs):
            self.provider.calls.append(kwargs)
            raise AuthenticationError('private key value')
        self.provider.decide_application_actions = fail
        snapshot = self.snapshot()
        self.bridge.save_snapshot(snapshot)
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        self.bridge.save_snapshot(snapshot)
        status = self.bridge.autopilot_status('42')
        self.assertEqual(status['modelFailureCode'], 'authentication')
        self.assertIn('API key was rejected', status['reason'])
        self.assertNotIn('private key value', status['reason'])
        self.assertEqual(len(self.provider.calls), 1)

    def test_disabled_submission_never_reaches_model_or_browser(self):
        self.bridge.save_snapshot(dict(tabId='42', url='https://example.com', documentKey='final', fields=[],
            finalActions=[dict(selector='final-action:submit:0', label='Submit')]))
        self.bridge.start_autopilot('42', {}, {}, auto_submit=False)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.bridge.autopilot_status('42')['state'], 'ready_to_submit')

    def test_no_supported_control_gets_model_clarification(self):
        self.provider.indexes = []
        self.bridge.save_snapshot(dict(tabId='42', url='https://example.com', documentKey='empty', fields=[]))
        self.bridge.start_autopilot('42', {}, {})
        self.assertEqual(self.provider.calls[0]['actions'], [])
        self.assertEqual(self.bridge.autopilot_status('42')['state'], 'needs_user_input')
        self.assertEqual(self.bridge.pending_commands('42'), [])

    def test_request_limit_stops_paid_calls_even_after_continue(self):
        self.provider.indexes = []
        self.bridge.save_snapshot(self.snapshot())
        self.bridge.start_autopilot('42', {'first_name': 'Test'}, {})
        self.bridge._autopilot['42']['model_call_limit'] = 2
        self.bridge.apply_instruction('42', 'Continue')
        for _ in range(3):
            self.bridge.apply_instruction('42', 'Continue')
        self.assertEqual(len(self.provider.calls), 2)
        status = self.bridge.autopilot_status('42')
        self.assertEqual(status['modelCalls'], 2)
        self.assertEqual(status['modelFailureCode'], 'request_limit')
        self.assertEqual(self.bridge.pending_commands('42'), [])


if __name__ == '__main__':
    unittest.main()
