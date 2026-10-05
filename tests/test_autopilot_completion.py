"""Exercise the saved-CV -> narrative -> submit -> confirmation path."""
import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from application_tracking import ApplicationTracker
from extension_bridge import ExtensionBridge, create_bridge_app


class AutopilotCompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "candidate.sqlite3"
        self.tracker = ApplicationTracker(self.database)
        self.tracker.save_profile({"skills": ["SQL", "Power BI"]})
        self.tracker.save_resume("resume.txt", "text/plain", b"Skills: SQL, Power BI. Data analysis.")
        self.bridge = ExtensionBridge(self.database)
        self.snapshot = {
            "tabId": "42", "url": "https://jobs.example.test/kraken/123/apply", "documentKey": "kraken-123",
            "title": "Data Analyst at Kraken", "body": "Data Analyst: SQL and Power BI",
            "fields": [
                {"selector": "#cv", "label": "Resume", "type": "file", "fileRole": "resume", "required": True, "value": "", "files": []},
                {"selector": "#why", "label": "Why are you interested in working at Kraken?", "type": "textarea", "required": True, "value": ""},
            ],
            "finalActions": [{"selector": "final-action:submit application:0", "label": "Submit application"}],
        }

    def start(self, auto_submit=True):
        self.bridge.save_snapshot(copy.deepcopy(self.snapshot))
        return self.bridge.start_autopilot("42", self.tracker.load_profile(), {}, auto_submit=auto_submit)

    def acknowledge(self, status):
        commands = self.bridge.pending_commands("42")
        self.assertEqual(len(commands), 1)
        self.bridge.add_result({"tabId": "42", "results": [{"commandId": commands[0]["commandId"], "selector": commands[0]["selector"], "status": status}]})
        return commands[0]

    def prepare(self):
        self.start()
        upload = self.acknowledge("uploaded")
        self.assertEqual(upload["kind"], "upload")
        self.assertEqual(upload["file"]["name"], "resume.txt")
        self.snapshot["fields"][0].update(value="resume.txt", files=["resume.txt"])
        self.bridge.save_snapshot(self.snapshot)
        narrative = self.acknowledge("applied")
        self.assertEqual(narrative["kind"], "fill")
        self.assertIn("Kraken", narrative["value"])
        self.assertIn("SQL", narrative["value"])
        self.snapshot["fields"][1]["value"] = narrative["value"]
        self.bridge.save_snapshot(self.snapshot)

    def test_saved_resume_and_kraken_answer_complete_without_user_answer(self):
        self.prepare()
        submission = self.acknowledge("submission_clicked")
        self.assertEqual(submission["kind"], "submit")
        self.assertTrue(submission["authorized"])
        self.assertEqual(self.tracker.submissions_today(), 0)
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.bridge.autopilot_status("42")["state"], "waiting_for_submission")
        self.assertEqual(self.bridge.pending_commands("42"), [])
        self.snapshot.update(submissionConfirmation=True, fields=[], finalActions=[])
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.bridge.autopilot_status("42")["state"], "submitted")
        self.assertEqual(self.tracker.submissions_today(), 1)

    def test_restarting_companion_does_not_repeat_unconfirmed_submission(self):
        self.prepare()
        self.acknowledge("submission_clicked")
        restarted = ExtensionBridge(self.database)
        restarted.save_snapshot(self.snapshot)
        status = restarted.start_autopilot("42", {}, {}, auto_submit=True)
        self.assertEqual(status["state"], "submission_unconfirmed")
        self.assertEqual(restarted.pending_commands("42"), [])

    def test_prepare_only_run_stops_at_ready(self):
        self.snapshot["fields"] = []
        status = self.start(auto_submit=False)
        self.assertEqual(status["state"], "ready_to_submit")
        with self.assertRaises(ValueError):
            self.bridge.queue_commands("42", [{"kind": "submit", **self.snapshot["finalActions"][0], "authorized": True}])

    def test_cover_letter_file_does_not_receive_resume(self):
        self.snapshot["fields"] = [{"selector": "#cover", "label": "Cover letter", "type": "file", "fileRole": "cover_letter", "required": True, "value": ""}]
        status = self.start()
        self.assertEqual(status["state"], "needs_user_input")
        self.assertEqual(self.bridge.pending_commands("42"), [])

    def test_unknown_fact_still_prevents_submission(self):
        self.snapshot["fields"] = [{"selector": "#fact", "label": "Current notice period", "type": "text", "required": True, "value": ""}]
        self.assertEqual(self.start()["state"], "needs_user_input")
        self.assertEqual(self.bridge.pending_commands("42"), [])

    def test_upload_failure_does_not_loop(self):
        self.start()
        self.acknowledge("unsupported")
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.bridge.autopilot_status("42")["state"], "action_failed")
        self.assertEqual(self.bridge.pending_commands("42"), [])

    def test_late_confirmation_after_timeout_is_recorded(self):
        self.prepare()
        self.acknowledge("submission_clicked")
        self.bridge._autopilot["42"]["submission_started"] -= 65
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.bridge.autopilot_status("42")["state"], "submission_unconfirmed")
        self.snapshot["submissionConfirmation"] = True
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.tracker.submissions_today(), 1)

    def test_post_submit_validation_accepts_correction_without_duplicate_submission(self):
        from unittest.mock import patch
        self.prepare()
        self.acknowledge('submission_clicked')
        email = dict(selector='#email', label='Email', type='email', required=True, value='')
        self.snapshot['fields'].append(email)
        self.snapshot['errors'] = ['Your form needs corrections. Missing entry for required field: Email']
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.bridge.autopilot_status('42')['state'], 'submission_unconfirmed')
        decision = dict(intent='answer', field=email, value='candidate@example.test')
        with patch('application_instructions.interpret_instruction', return_value=decision) as interpreter:
            reply = self.bridge.apply_instruction('42', "it's candidate@example.test")
            interpreter.assert_called_once()
        self.assertEqual(reply['interpretation']['field'], 'Email')
        command = self.acknowledge('applied')
        self.assertEqual(command['kind'], 'fill')
        self.assertEqual(command['selector'], '#email')
        email['value'] = 'candidate@example.test'
        self.snapshot['errors'] = []
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.bridge.pending_commands('42'), [])
        self.assertEqual(self.bridge.autopilot_status('42')['state'], 'submission_unconfirmed')
        self.assertTrue(self.tracker.submission_record(self.snapshot['url'])['claimed'])
        self.snapshot['submissionConfirmation'] = True
        self.bridge.save_snapshot(self.snapshot)
        self.assertEqual(self.tracker.submissions_today(), 1)

    def test_pending_submission_message_does_not_request_restart_or_call_model(self):
        from unittest.mock import patch
        self.prepare()
        self.acknowledge('submission_clicked')
        with patch('application_instructions.interpret_instruction') as interpreter:
            with self.assertRaisesRegex(ValueError, 'awaiting confirmation'):
                self.bridge.apply_instruction('42', 'I need to correct my details')
            interpreter.assert_not_called()

    def test_required_manual_question_blocks_submission_before_any_click(self):
        self.snapshot['fields'] = [dict(selector='#sponsor', label='Will you require sponsorship?',
                                        type='select-one', required=True, value='')]
        self.assertEqual(self.start()['state'], 'needs_user_input')
        self.assertEqual(self.bridge.pending_commands('42'), [])

    def test_chrome_filled_login_can_continue_without_exporting_credentials(self):
        self.snapshot.update(fields=[], finalActions=[], accessReady=True, accessPending=False,
                             accessActions=[{"selector": "access-action:sign in:0", "label": "Sign in"}])
        self.start()
        command = self.bridge.pending_commands("42")[0]
        self.assertEqual(command["kind"], "authenticate")
        self.assertTrue(command["authorized"])
        self.assertEqual(command["value"], "")

    def test_api_uses_its_own_database_and_rejects_employer_origin(self):
        self.snapshot["fields"] = []
        self.bridge.save_snapshot(self.snapshot)
        with TestClient(create_bridge_app(self.bridge)) as client:
            rejected = client.post("/v1/autopilot/42", json={"autoSubmit": True}, headers={"Origin": "https://employer.example"})
            self.assertEqual(rejected.status_code, 403)
            accepted = client.post("/v1/autopilot/42", json={"autoSubmit": False}, headers={"Origin": "chrome-extension://" + "a" * 32})
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json()["status"]["state"], "ready_to_submit")


if __name__ == "__main__":
    unittest.main()
