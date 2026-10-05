import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from application_tracking import ApplicationTracker
from autonomous_application import AutomationPolicy, ChromeApplicationAgent


PROFILE = {
    "first_name": "Deepansh",
    "last_name": "Manchanda",
    "email": "deepanshmanchanda@gmail.com",
    "phone": "+44 7570240884",
    "location": "London, United Kingdom",
    "city": "London",
}
ANSWER_LIBRARY = {"role": "BI Analyst", "company": "Example Ltd"}
COMPLETE_FORM = """<form>
<label>First name <input required name="firstName"></label>
<label>Last name <input required name="lastName"></label>
<label>Email <input required type="email" name="email"></label>
<button type="submit" onclick="event.preventDefault(); document.body.innerHTML='<p>Thank you for applying</p>'">Submit application</button>
</form>"""
MISSING_FORM = """<form><label>Right to work<input required name="rightToWork"></label><button type="submit">Submit</button></form>"""


class AutonomousApplicationTests(unittest.TestCase):
    def test_tabs_only_connection_can_list_and_open_pasted_links(self):
        agent = ChromeApplicationAgent()
        agent.tabs_only = True
        agent._remote_pages = lambda: [{
            "title": "Data Quality Analyst | Citi Careers",
            "url": "https://jobs.example.test/data-quality-analyst",
        }]
        agent._new_remote_page = lambda url: {
            "title": "New Analyst Role | Example",
            "url": url,
        }
        self.assertEqual(agent.current_pages()[0]["title"], "Data Quality Analyst | Citi Careers")
        selected = agent.inspect_job_tab("https://jobs.example.test/data-quality-analyst")
        self.assertEqual(selected["role"], "Data Quality Analyst")
        opened = agent.open_and_inspect_job_link("https://jobs.example.test/new-role")
        self.assertEqual(opened["url"], "https://jobs.example.test/new-role")

    def test_hybrid_requires_sponsorship_evidence_when_configured(self):
        policy = AutomationPolicy(mode="hybrid", final_submission_enabled=True, hybrid_minimum_score=80)
        allowed, reason = policy.can_submit({"score": 90, "require_sponsorship": True, "sponsorship_status": "unknown"})
        self.assertFalse(allowed)
        self.assertIn("Sponsorship", reason)

    def test_review_mode_never_auto_progresses(self):
        allowed, reason = AutomationPolicy(mode="review", final_submission_enabled=True).can_auto_progress({"score": 100})
        self.assertFalse(allowed)
        self.assertIn("Review mode", reason)

    def test_agent_submits_only_after_visible_confirmation(self):
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as playwright:
            tracker = ApplicationTracker(Path(folder) / "applications.sqlite3")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(COMPLETE_FORM)
            agent = ChromeApplicationAgent()
            agent.context = object()  # The test supplies a local form instead of a CDP Chrome tab.
            agent.open_or_select = lambda _url: page
            result = agent.run(
                url="https://example.com/apply",
                role="BI Analyst",
                company="Example Ltd",
                profile=PROFILE,
                answers=ANSWER_LIBRARY,
                resume=None,
                job={"score": 100, "require_sponsorship": True, "sponsorship_status": "evidence_found"},
                policy=AutomationPolicy(mode="auto", final_submission_enabled=True),
                tracker=tracker,
            )
            self.assertEqual(result["status"], "submitted")
            self.assertEqual(tracker.list_applications()[0]["status"], "submitted")
            self.assertEqual(tracker.automation_events(result["application_id"])[0]["event"], "submission_confirmed")
            browser.close()

    def test_agent_stops_when_required_fact_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as playwright:
            tracker = ApplicationTracker(Path(folder) / "applications.sqlite3")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(MISSING_FORM)
            agent = ChromeApplicationAgent()
            agent.context = object()
            agent.open_or_select = lambda _url: page
            result = agent.run(
                url="https://example.com/right-to-work",
                role="BI Analyst",
                company="Example Ltd",
                profile=PROFILE,
                answers=ANSWER_LIBRARY,
                resume=None,
                job={"score": 100, "require_sponsorship": False, "sponsorship_status": "unknown"},
                policy=AutomationPolicy(mode="auto", final_submission_enabled=True),
                tracker=tracker,
            )
            self.assertEqual(result["status"], "needs_user_input")
            self.assertIn("Right to work", result["fields"][0])
            self.assertNotEqual(tracker.list_applications()[0]["status"], "submitted")
            browser.close()

    def test_agent_enforces_confirmation_backed_daily_cap(self):
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as playwright:
            tracker = ApplicationTracker(Path(folder) / "applications.sqlite3")
            prior_id = tracker.save_form_plan("https://example.com/already-submitted", "BI Analyst", "Example Ltd", [])
            tracker.mark_submitted(prior_id)
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(COMPLETE_FORM)
            agent = ChromeApplicationAgent()
            agent.context = object()
            agent.open_or_select = lambda _url: page
            result = agent.run(
                url="https://example.com/capped",
                role="BI Analyst",
                company="Example Ltd",
                profile=PROFILE,
                answers=ANSWER_LIBRARY,
                resume=None,
                job={"score": 100, "require_sponsorship": False, "sponsorship_status": "unknown"},
                policy=AutomationPolicy(mode="auto", final_submission_enabled=True, daily_limit=1),
                tracker=tracker,
            )
            self.assertEqual(result["status"], "ready_to_submit")
            self.assertIn("Daily submission cap", result["reason"])
            self.assertEqual(tracker.submissions_today(), 1)
            browser.close()


if __name__ == "__main__":
    unittest.main()
