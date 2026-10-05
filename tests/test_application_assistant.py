import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from application_tracking import ApplicationTracker
from job_assistant_rebuilt import ANSWER_DRAFTS, apply_reviewed_plan, inspect_application_form, profile_suggestion, requires_manual_access


PROFILE = {
    "first_name": "Deepansh",
    "last_name": "Manchanda",
    "email": "deepanshmanchanda@gmail.com",
    "phone": "+44 7570240884",
    "location": "London, United Kingdom",
    "city": "London",
    "state": "",
    "postal_code": "",
    "linkedin": "https://www.linkedin.com/in/deepansh-manchanda-b547803b",
    "website": "",
}

FORM_HTML = """<form>
<label>Title *<select required name="title"><option value="">Please select</option><option>Mr</option><option>Ms</option></select></label>
<label>First Name *<input required name="firstName"></label>
<label>Surname *<input required name="surname"></label>
<label>Address *<input required name="address" value="London, United Kingdom"></label>
<label>Town *<input required name="town"></label>
<label>County *<input required name="county" value="London, United Kingdom"></label>
<label>Mobile *<input required name="mobile"></label>
<label>Alternate Contact Number<input name="alternatePhone" value="+44 7570240884"></label>
<label>What attracts you to this role and working for RHP? *<textarea required name="motivation"></textarea></label>
<label>Tell us how your knowledge, skills and experience would make you shine in this role. *<textarea required name="skills"></textarea></label>
<label>Tell us about a time you found a better way of doing something. *<textarea required name="improvement"></textarea></label>
<label>Tell us about a time you received amazing service. *<textarea required name="service"></textarea></label>
<label>Upload CV<input type="file" aria-label="Upload CV"></label>
<button type="submit" onclick="window.submitted=true">Submit</button></form>"""


class ApplicationAssistantTests(unittest.TestCase):
    def test_login_page_is_recognised_as_manual_access(self):
        self.assertTrue(requires_manual_access("Log In", "https://careers.example.test/login"))

    def test_second_address_line_is_not_populated_with_the_street_address(self):
        profile = {"street_address": "75 Trinity Gardens", "address_line_2": ""}
        self.assertIsNone(profile_suggestion({"labels": "Address Line 2"}, profile))

    def test_country_uses_country_of_residence_not_city(self):
        profile = {"country": "United Kingdom", "city": "London"}
        self.assertEqual(profile_suggestion({"labels": "Country"}, profile), "United Kingdom")

    def test_country_of_residence_uses_the_saved_country_not_the_freeform_location(self):
        profile = {"country": "United Kingdom", "location": "London, United Kingdom"}
        self.assertEqual(profile_suggestion({"labels": "Country of Residence"}, profile), "United Kingdom")

    def test_declaration_questions_are_never_inferred_from_generic_profile_words(self):
        profile = {"country": "United Kingdom"}
        self.assertIsNone(profile_suggestion({"labels": "Are you legally entitled to work in the country you are applying for?"}, profile))
        self.assertIsNone(profile_suggestion({"labels": "Country of Application"}, profile))

    def test_form_plan_maps_facts_and_surfaces_unknowns(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(FORM_HTML)
            plan = inspect_application_form(page, PROFILE, ANSWER_DRAFTS, {"name": "resume.docx"})
            actions = {field["field"]: field for field in plan["fields"]}
            self.assertEqual(actions["Title *"]["action"], "review")
            self.assertEqual(actions["Town *"]["proposed_value"], "London")
            self.assertEqual(actions["Address *"]["action"], "clear")
            self.assertEqual(actions["County *"]["action"], "clear")
            self.assertEqual(actions["Alternate Contact Number"]["action"], "clear")
            self.assertEqual(actions["Tell us about a time you received amazing service. *"]["action"], "review")
            self.assertIn("Title *", plan["required_missing"])
            self.assertTrue(plan["resume_available"])
            browser.close()

    def test_apply_plan_fills_reviewed_values_without_submission(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(FORM_HTML)
            plan = inspect_application_form(page, PROFILE, ANSWER_DRAFTS, {"name": "resume.docx"})
            applied, cleared, attached, missing = apply_reviewed_plan(
                page,
                plan,
                {
                    "name": "resume.docx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "data": b"test docx",
                },
            )
            self.assertEqual(page.locator('[name="firstName"]').input_value(), "Deepansh")
            self.assertEqual(page.locator('[name="town"]').input_value(), "London")
            self.assertEqual(page.locator('[name="title"]').input_value(), "")
            self.assertEqual(page.locator('[name="service"]').input_value(), "")
            self.assertEqual(page.locator('[name="alternatePhone"]').input_value(), "")
            self.assertEqual(page.locator('[name="motivation"]').input_value(), ANSWER_DRAFTS["motivation"].format(role="this role", company="the organisation"))
            self.assertTrue(attached)
            self.assertIn("Title *", missing)
            self.assertIn("Alternate Contact Number", cleared)
            self.assertFalse(page.evaluate("window.submitted || false"))
            browser.close()

    def test_application_ledger_and_answer_library_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker = ApplicationTracker(Path(folder) / "applications.sqlite3")
            record_id = tracker.save_form_plan(
                "https://example.com/apply", "BI Analyst", "Example Ltd",
                [{"field": "First name", "value": "Deepansh", "source": "profile"}],
                candidate_skills=["SQL", "Power BI"],
                required_skills=["SQL", "Snowflake"],
                skill_gaps=["Snowflake"],
            )
            tracker.save_answer("skills", "Relevant skills", "SQL and Power BI", reviewed=True)
            record = tracker.list_applications()[0]
            self.assertEqual(record["id"], record_id)
            self.assertEqual(record["role"], "BI Analyst")
            self.assertEqual(record["company"], "Example Ltd")
            self.assertEqual(record["skill_gaps"], ["Snowflake"])
            self.assertEqual(record["form_snapshot"][0]["value"], "Deepansh")
            self.assertEqual(tracker.answer_library()["skills"]["response"], "SQL and Power BI")
            tracker.mark_submitted(record_id)
            self.assertEqual(tracker.list_applications()[0]["status"], "submitted")

    def test_candidate_profile_and_resume_persist_across_store_instances(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "applications.sqlite3"
            tracker = ApplicationTracker(path)
            tracker.save_profile({"first_name": "Deepansh", "city": "London"})
            tracker.save_resume("cv.docx", "application/docx", b"resume bytes")
            reopened = ApplicationTracker(path)
            self.assertEqual(reopened.load_profile(), {"first_name": "Deepansh", "city": "London"})
            self.assertEqual(reopened.load_resume(), {"name": "cv.docx", "mime_type": "application/docx", "data": b"resume bytes"})


if __name__ == "__main__":
    unittest.main()
