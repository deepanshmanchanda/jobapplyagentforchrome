import tempfile
import unittest
from pathlib import Path
from application_tracking import ApplicationTracker
from extension_bridge import ExtensionBridge, choose_progress_action
from job_assistant_rebuilt import profile_suggestion
from workday_employment import resume_employment, workday_employment_values


CV = '''PROFESSIONAL EXPERIENCE
Example UK
Assistant Manager - Analytics | Technical Lead | March 2023 - Present
Delivered analytics.
Previous Ltd
Analyst | March 2021 - February 2023
EDUCATION
University
'''


class WorkdayEmploymentTests(unittest.TestCase):
    def snapshot(self):
        return {"tabId": "42", "url": "https://example.myworkdayjobs.com/apply", "documentKey": "employment",
                "fields": [{"selector": "#workExperience-5--" + suffix, "label": label,
                            "type": "checkbox" if suffix == "currentlyWorkHere" else "text",
                            "required": suffix in {"jobTitle", "companyName"}, "value": value}
                           for suffix, label, value in [
                               ("jobTitle", "Job Title*", ""), ("companyName", "Company*", "Test Candidate"),
                               ("currentlyWorkHere", "I currently work here", ""),
                               ("startDate-dateSectionMonth-input", "Month", ""),
                               ("startDate-dateSectionYear-input", "Year", "")]]}

    def test_parses_explicit_title_company_and_dates(self):
        records = resume_employment(CV)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["title"], "Assistant Manager - Analytics | Technical Lead")
        self.assertEqual(records[0]["company"], "Example UK")
        self.assertEqual(records[0]["start_month"], "3")
        self.assertTrue(records[0]["current"])
        self.assertEqual(records[1]["end_year"], "2023")

    def test_bridge_fills_workday_from_cv_and_repairs_personal_name_as_company(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "jobs.sqlite3"
            tracker = ApplicationTracker(database)
            tracker.save_resume("resume.txt", "text/plain", CV.encode())
            bridge = ExtensionBridge(database)
            bridge.save_snapshot(self.snapshot())
            bridge.start_autopilot("42", {"first_name": "Test", "last_name": "Candidate"}, {})
            check = bridge.pending_commands("42")[0]
            self.assertEqual(check["kind"], "check")
            self.assertEqual(check["selector"], "#workExperience-5--currentlyWorkHere")
            bridge.add_result({"tabId": "42", "results": [{"commandId": check["commandId"], "status": "applied"}]})
            snapshot = self.snapshot()
            snapshot["fields"][2]["value"] = "on"
            bridge.save_snapshot(snapshot)
            commands = {item["selector"].split("--", 1)[1]: item for item in bridge.pending_commands("42")}
            self.assertEqual(commands["companyName"]["value"], "Example UK")
            self.assertEqual(commands["jobTitle"]["value"], "Assistant Manager - Analytics | Technical Lead")
            self.assertEqual(commands["startDate-dateSectionYear-input"]["value"], "2023")

    def test_does_not_replace_unknown_employer_with_latest_role(self):
        snapshot = self.snapshot()
        snapshot["fields"][1]["value"] = "Another employer"
        self.assertEqual(workday_employment_values(snapshot, CV, {}), {})

    def test_company_name_never_receives_candidate_name(self):
        self.assertIsNone(profile_suggestion({"labels": "Company*", "id": "companyName"}, {"first_name": "Test", "last_name": "Candidate"}))

    def test_apply_for_job_is_selected_without_form_controls(self):
        action = {"label": "Apply for this job", "selector": "safe-action:apply for this job:0"}
        self.assertEqual(choose_progress_action([action]), action)
        self.assertIsNone(choose_progress_action([action, {**action, "selector": "other"}]))


if __name__ == '__main__':
    unittest.main()
