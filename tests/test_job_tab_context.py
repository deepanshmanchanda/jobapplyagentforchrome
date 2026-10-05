import unittest

from autonomous_application import job_context_from_page


class JobTabContextTests(unittest.TestCase):
    def test_networx_context_prefills_role_company_and_description(self):
        context = job_context_from_page(
            "https://example.test/jobs/123",
            "Business Intelligence Performance Analyst",
            "Hello. We’re RHP. Our purpose is providing safe, secure homes.",
        )
        self.assertEqual(context["url"], "https://example.test/jobs/123")
        self.assertEqual(context["role"], "Business Intelligence Performance Analyst")
        self.assertEqual(context["company"], "RHP")
        self.assertIn("safe, secure homes", context["description"])

    def test_context_keeps_company_blank_when_the_page_does_not_identify_one(self):
        context = job_context_from_page("https://example.test", "Data Analyst | Careers", "Role overview")
        self.assertEqual(context["role"], "Data Analyst")
        self.assertEqual(context["company"], "")


if __name__ == "__main__":
    unittest.main()
