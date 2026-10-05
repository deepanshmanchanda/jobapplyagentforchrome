import tempfile
import unittest
from pathlib import Path

from application_tracking import ApplicationTracker
from extension_bridge import ExtensionBridge, choose_progress_action, is_safe_click
from workday_employment import workday_employment_values


CV = '''PROFESSIONAL EXPERIENCE
First Ltd
Engineer | March 2023 - Present
Second Ltd
Analyst | March 2021 - February 2023
EDUCATION
Bachelor of Engineering | Example University | 2017 - 2021
'''


class ResumeSectionTests(unittest.TestCase):
    def test_natural_degree_reply_is_structured_and_remembered_for_record(self):
        from extension_bridge import load_saved_candidate_context
        from workday_qualifications import qualification_plan
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'jobs.sqlite3'
            tracker = ApplicationTracker(database)
            tracker.save_resume('resume.txt', 'text/plain', CV.encode())
            bridge = ExtensionBridge(database)
            snapshot = dict(tabId='42', url='https://example.myworkdayjobs.com/apply', documentKey='form',
                resumeSections=[dict(kind='education', add=None)], fields=[
                dict(selector='#school', label='School', type='text', value='Example University', resumeSection='education', resumeRow='education-1'),
                dict(selector='#degree', label='Degree*', type='combobox', value='', resumeSection='education', resumeRow='education-1', options=[])])
            bridge.save_snapshot(snapshot)
            bridge.start_autopilot('42', {}, {})
            result = bridge.apply_instruction('42', "it's a technical diploma", remember=True)
            self.assertEqual(result['interpretation'], dict(field='Degree', value='technical diploma'))
            self.assertIn('Remembered for this education record', result['message'])
            _, answers = load_saved_candidate_context(database)
            self.assertEqual(len(answers), 1)
            plan = qualification_plan(snapshot, tracker.load_resume(), answers)
            self.assertEqual(plan['values']['#degree']['value'], 'technical diploma')
            other_resume = CV.replace('Example University', 'Another University')
            snapshot['fields'][0]['value'] = 'Another University'
            other_plan = qualification_plan(snapshot, other_resume, answers)
            self.assertEqual(other_plan['values']['#degree']['value'], 'Bachelor of Engineering')

    def test_context_reply_selects_unique_option_and_preserves_user_value(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = ExtensionBridge(Path(folder) / 'jobs.sqlite3')
            snapshot = dict(tabId='42', url='https://example.myworkdayjobs.com/apply', documentKey='form', fields=[
                dict(selector='#degree', label='Degree*', type='combobox', value='',
                     resumeSection='education', resumeRow='education-1', options=['Other', 'Technical Diploma']),
                dict(selector='#study', label='Field of Study', type='combobox', value='',
                     resumeSection='education', resumeRow='education-1', options=['Accounting'])])
            bridge.save_snapshot(snapshot)
            bridge.start_autopilot('42', {}, {})
            bridge.apply_instruction('42', 'its technical diploma', remember=True)
            commands = bridge.pending_commands('42')
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]['selector'], '#degree')
            self.assertEqual(commands[0]['kind'], 'resume-select')
            self.assertEqual(commands[0]['aliases'], ['technical diploma'])
            self.assertEqual(ApplicationTracker(Path(folder) / 'jobs.sqlite3').answer_library(), {})

    def test_ambiguous_reply_requests_field_without_queuing(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = ExtensionBridge(Path(folder) / 'jobs.sqlite3')
            bridge.save_snapshot(dict(tabId='42', url='https://example.com', documentKey='form', fields=[
                dict(selector='#one', label='Degree', type='select-one', value='', options=['Other']),
                dict(selector='#two', label='School', type='select-one', value='', options=['Other'])]))
            bridge.start_autopilot('42', {}, {})
            with self.assertRaisesRegex(ValueError, 'Which field'):
                bridge.apply_instruction('42', 'use Other')
            self.assertEqual(bridge.pending_commands('42'), [])

    def test_failure_identifies_control_without_echoing_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = ExtensionBridge(Path(folder) / 'jobs.sqlite3')
            bridge.save_snapshot(dict(tabId='42', url='https://example.com/apply', documentKey='form', fields=[
                dict(selector='#first', label='First name', type='text', value='')]))
            bridge.start_autopilot('42', {'first_name': 'PrivateCandidateValue'}, {})
            command = bridge.pending_commands('42')[0]
            bridge.add_result({'tabId': '42', 'results': [dict(commandId=command['commandId'], status='unsupported')]})
            status = bridge.autopilot_status('42')
            self.assertEqual(status['state'], 'action_failed')
            self.assertIn('First name (unsupported)', status['reason'])
            self.assertIn('adapter', status['reason'])
            self.assertNotIn('PrivateCandidateValue', status['reason'])

    def test_employer_entry_labels_are_accepted_by_backend(self):
        for label in ['Apply', 'Apply online', 'Apply for this position', 'Apply for the role', 'Start your application']:
            action = dict(selector='safe-action:' + label.lower() + ':0', label=label)
            self.assertTrue(is_safe_click(label))
            self.assertEqual(choose_progress_action([action]), action)

    def test_stale_field_batch_replans_with_bounded_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = ExtensionBridge(Path(folder) / 'jobs.sqlite3')
            snapshot = dict(tabId='42', url='https://example.com/apply', documentKey='form', fields=[
                dict(selector='#first', label='First name', type='text', value='')])
            bridge.save_snapshot(snapshot)
            bridge.start_autopilot('42', {'first_name': 'Test'}, {})
            for attempt in range(3):
                command = bridge.pending_commands('42')[0]
                bridge.add_result({'tabId': '42', 'results': [dict(commandId=command['commandId'], status='stale_document')]})
                self.assertEqual(bridge.autopilot_status('42')['state'],
                                 'waiting_for_snapshot' if attempt < 2 else 'action_failed')
                bridge.save_snapshot(snapshot)
            self.assertEqual(bridge.pending_commands('42'), [])

    def test_add_then_fill_education_and_do_not_repeat_add(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'jobs.sqlite3'
            ApplicationTracker(database).save_resume('resume.txt', 'text/plain', CV.encode())
            bridge = ExtensionBridge(database)
            snapshot = dict(tabId='42', url='https://example.myworkdayjobs.com/apply',
                            documentKey='empty', fields=[], resumeSections=[{
                                'kind': 'education', 'add': {'selector': '#add', 'label': 'Add'}}])
            bridge.save_snapshot(snapshot)
            bridge.start_autopilot('42', {}, {})
            command = bridge.pending_commands('42')[0]
            self.assertEqual(command['kind'], 'resume-add')
            self.assertEqual(command['rowCount'], 0)
            bridge.add_result({'tabId': '42', 'results': [{'commandId': command['commandId'], 'status': 'clicked'}]})
            bridge.save_snapshot(snapshot)
            self.assertEqual(bridge.pending_commands('42'), [])
            snapshot.update(documentKey='row', fields=[{
                'selector': '#education-1--school', 'label': 'School', 'type': 'text',
                'resumeSection': 'education', 'resumeRow': 'education-1', 'required': True, 'value': ''}])
            bridge.save_snapshot(snapshot)
            command = bridge.pending_commands('42')[0]
            self.assertEqual(command['value'], 'Example University')
            self.assertEqual(command['kind'], 'fill')

    def test_add_is_rejected_outside_supported_site(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = ExtensionBridge(Path(folder) / 'jobs.sqlite3')
            bridge.save_snapshot(dict(tabId='42', url='https://example.com', fields=[], resumeSections=[{
                'kind': 'education', 'add': {'selector': '#add', 'label': 'Add'}}]))
            with self.assertRaises(ValueError):
                bridge.queue_commands('42', [dict(kind='resume-add', selector='#add', label='Add',
                                                  section='education', rowCount=0, authorized=True)])

    def test_second_empty_employment_row_uses_next_resume_record(self):
        fields = []
        for row, company, title in [('1', 'First Ltd', 'Engineer'), ('2', '', '')]:
            for suffix, value in [('companyName', company), ('jobTitle', title)]:
                fields.append(dict(selector=f'#workExperience-{row}--{suffix}', value=value))
        values = workday_employment_values(dict(url='https://example.myworkdayjobs.com', fields=fields), CV, {})
        self.assertEqual(values['#workExperience-2--companyName'], 'Second Ltd')
        self.assertEqual(values['#workExperience-2--jobTitle'], 'Analyst')


if __name__ == '__main__':
    unittest.main()
