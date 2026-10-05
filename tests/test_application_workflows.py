import unittest

from application_workflows import validate_employment_history, workflow_for_url


class ApplicationWorkflowTests(unittest.TestCase):
    def test_oracle_workflow_is_detected(self):
        workflow = workflow_for_url("https://example.opc.oracleoutsourcing.com/ords/r/recruitment/application")
        self.assertEqual(workflow["id"], "oracle_recruiting")
        self.assertTrue(workflow["can_attach_cv"])
        self.assertTrue(workflow["can_add_employment"])

    def test_month_only_employment_date_is_not_accepted(self):
        records, problems = validate_employment_history([{
            "employer": "Example Ltd", "title": "Analyst", "start_date": "2023-03",
            "end_date": "", "current": True, "description": "",
        }])
        self.assertEqual(records, [])
        self.assertIn("exact start date", problems[0])

    def test_verified_current_employment_record_is_accepted(self):
        records, problems = validate_employment_history([{
            "employer": "Example Ltd", "title": "Analyst", "start_date": "2023-03-01",
            "end_date": "", "current": True, "description": "Verified description",
        }])
        self.assertEqual(problems, [])
        self.assertEqual(records[0]["employer"], "Example Ltd")
        self.assertTrue(records[0]["current"])


if __name__ == "__main__":
    unittest.main()
