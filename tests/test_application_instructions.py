import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_field_memory import parse_agent_instruction
from application_instructions import interpret_instruction
from application_tracking import ApplicationTracker
from extension_bridge import ExtensionBridge, load_saved_candidate_context
from workday_qualifications import qualifications, qualification_plan, education_selection_key

CV = '''EDUCATION
Postgraduate Diploma | Illinois | 2025
Bachelor of Engineering (Computer Science) | Engineering University | 2010 - 2014
'''
REQUEST = 'Do not add illenois as education just add my engineering degree'


class InstructionTests(unittest.TestCase):
    def snapshot(self):
        return dict(tabId='42', url='https://example.myworkdayjobs.com/apply', documentKey='form',
            resumeSections=[dict(kind='education', add=dict(selector='#add', label='Add Another'))], fields=[
                dict(selector='#school', label='School or University', type='combobox', value='Illinois', resumeSection='education', resumeRow='education-1'),
                dict(selector='#degree', label='Degree', type='combobox', value='Technical Diploma', resumeSection='education', resumeRow='education-1')])

    def test_negation_is_workflow_not_answer(self):
        self.assertEqual(parse_agent_instruction(REQUEST)[0], 'workflow')
        self.assertEqual(parse_agent_instruction('Only use my engineering degree then continue')[0], 'workflow')

    def test_model_cannot_return_workflow_as_field_value(self):
        from unittest.mock import Mock
        provider = Mock()
        provider.interpret_application_instruction.return_value = dict(intent='answer', field_selector='#school', value=REQUEST)
        with self.assertRaises(ValueError):
            interpret_instruction(REQUEST, self.snapshot(), CV, provider)
        provider.interpret_application_instruction.return_value = dict(intent='education_selection', education_indexes=[9])
        with self.assertRaises(ValueError):
            interpret_instruction(REQUEST, self.snapshot(), CV, provider)

    def test_education_selection_replaces_prior_row_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'jobs.sqlite3'
            tracker = ApplicationTracker(database)
            tracker.save_resume('resume.txt', 'text/plain', CV.encode())
            bridge = ExtensionBridge(database)
            snapshot = self.snapshot()
            bridge.save_snapshot(snapshot)
            bridge.start_autopilot('42', {}, {})
            chosen = [qualifications(CV)['education'][1]]
            with patch('application_instructions.interpret_instruction', return_value=dict(intent='education_selection', records=chosen)):
                reply = bridge.apply_instruction('42', REQUEST, remember=True)
            self.assertIn('Bachelor of Engineering', reply['message'])
            commands = bridge.pending_commands('42')
            self.assertEqual(commands[0]['value'], 'Engineering University')
            self.assertNotIn(REQUEST, str(commands))
            _, answers = load_saved_candidate_context(database)
            self.assertIn(education_selection_key(snapshot['url'], CV), answers)
            restarted = ExtensionBridge(database)
            restarted.save_snapshot(snapshot)
            restarted.start_autopilot('42', {}, answers)
            self.assertEqual(restarted.pending_commands('42')[0]['value'], 'Engineering University')
            plan = qualification_plan(snapshot, CV, selected_education=chosen)
            self.assertEqual(plan['values']['#degree']['value'], 'Bachelor of Engineering')
            self.assertIsNone(plan['add'])

    def test_interpreter_failure_never_falls_back_to_pasting_instruction(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = ExtensionBridge(Path(folder) / 'jobs.sqlite3')
            bridge.save_snapshot(self.snapshot())
            bridge.start_autopilot('42', {}, {})
            with patch('application_instructions.interpret_instruction', side_effect=RuntimeError('private error')):
                with self.assertRaisesRegex(ValueError, 'No instruction text'):
                    bridge.apply_instruction('42', REQUEST)
            self.assertEqual(bridge.pending_commands('42'), [])


if __name__ == '__main__':
    unittest.main()
