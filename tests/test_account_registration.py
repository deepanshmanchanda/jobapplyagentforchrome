"""Local browser registration and Windows-encrypted credential integration."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from playwright.sync_api import sync_playwright
from application_tracking import ApplicationTracker
from extension_bridge import ExtensionBridge
from registration_credentials import account_password
from test_extension_application_actions import CONTENT, RUNTIME


@unittest.skipUnless(os.name == "nt", "Windows DPAPI credential integration")
class AccountRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "jobs.sqlite3"
        self.tracker = ApplicationTracker(self.database)
        self.profile = {"first_name": "Test", "email": "candidate@example.test"}
        self.bridge = ExtensionBridge(self.database)
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.route("https://employer.example.test/**", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
        self.page.goto("https://employer.example.test/apply")

    def install(self, extra=""):
        self.page.set_content('''<h1>Create account</h1>
          <form onsubmit="event.preventDefault();window.created=(window.created||0)+1">
          <label for=first>First name</label><input id=first required>
          <label for=email>Email</label><input id=email type=email required>
          <label for=password>Password</label><input id=password type=password required minlength=12>
          <label for=confirm>Confirm password</label><input id=confirm type=password required>
          ''' + extra + '''<button>Create account</button></form>''')
        self.page.evaluate(RUNTIME)
        self.page.add_script_tag(content=CONTENT)

    def snapshot(self):
        snapshot = self.page.evaluate("() => window.__request({type:'application-snapshot'})")
        snapshot["tabId"] = "12"
        return snapshot

    def sync(self):
        return self.bridge.save_snapshot(self.snapshot())

    def execute(self):
        commands = self.bridge.pending_commands("12")
        self.assertTrue(commands)
        results = self.page.evaluate("commands => window.__request({type:'application-commands', commands})", commands)
        self.bridge.add_result({"tabId": "12", "results": results})
        self.assertTrue(all(result["status"] in {"applied", "clicked", "submission_clicked"} for result in results), results)
        return commands, results

    def start(self, enabled=True):
        self.sync()
        return self.bridge.start_autopilot("12", self.profile, {}, auto_register=enabled, auto_submit=True)

    def test_registration_to_application_submission_keeps_password_out_of_snapshots(self):
        self.install()
        self.start()
        self.execute()  # saved profile
        self.sync()
        commands, results = self.execute()  # generated password
        password = commands[0]["password"]
        self.assertEqual(commands[0]["phase"], "prepare")
        self.assertEqual(self.page.locator("#password").input_value(), password)
        self.assertEqual(self.page.locator("#confirm").input_value(), password)
        self.assertNotIn(password, json.dumps(self.snapshot()))
        self.assertNotIn(password, json.dumps(results))
        self.sync()
        commands, _ = self.execute()
        self.assertEqual(commands[0]["phase"], "create")
        self.assertEqual(self.page.evaluate("window.created"), 1)
        self.assertNotIn(password.encode(), self.database.read_bytes())
        self.assertEqual(account_password(self.database, self.page.url, self.profile["email"]), password)
        self.page.set_content('''<h1>Application</h1><form onsubmit="event.preventDefault();document.body.innerHTML='<h1>Thank you for applying</h1>'"><button>Submit application</button></form>''')
        self.sync()
        self.execute()
        self.sync()
        self.assertEqual(self.bridge.autopilot_status("12")["state"], "submitted")

    def test_disabled_registration_never_generates_credentials(self):
        self.install()
        self.start(enabled=False)
        self.execute()
        self.sync()
        self.assertEqual(self.bridge.autopilot_status("12")["state"], "needs_user_input")
        self.assertEqual(self.bridge.pending_commands("12"), [])
        self.assertEqual(self.page.locator("#password").input_value(), "")

    def test_required_terms_are_left_for_user(self):
        self.install('<label><input type=checkbox required>I agree to the terms</label>')
        self.start()
        self.execute()
        self.sync()
        self.assertEqual(self.bridge.autopilot_status("12")["state"], "needs_user_input")
        self.assertFalse(self.page.locator("input[type=checkbox]").is_checked())
        self.assertIsNone(self.page.evaluate("window.created"))

    def test_changed_email_blocks_password_command(self):
        self.install()
        self.start()
        self.execute()
        self.sync()
        self.page.locator("#email").fill("someoneelse@example.test")
        commands = self.bridge.pending_commands("12")
        results = self.page.evaluate("commands => window.__request({type:'application-commands', commands})", commands)
        self.assertEqual(results[0]["status"], "blocked")
        self.assertEqual(self.page.locator("#password").input_value(), "")

    def test_passwords_are_unique_by_employer_and_encrypted_on_disk(self):
        one = account_password(self.database, "https://one.example.test/register", self.profile["email"])
        two = account_password(self.database, "https://two.example.test/register", self.profile["email"])
        self.assertNotEqual(one, two)
        self.assertEqual(account_password(self.database, "https://one.example.test/login", self.profile["email"]), one)
        self.assertNotIn(one.encode(), self.database.read_bytes())
        with self.assertRaises(ValueError):
            account_password(self.database, "http://one.example.test", self.profile["email"])

    def test_registration_link_is_navigation_and_requires_authorization(self):
        self.install()
        self.page.set_content('<h1>Sign in to apply</h1><a href="/register">Create account</a>')
        snapshot = self.snapshot()
        self.assertEqual(snapshot["registrationActions"][0]["phase"], "open")
        self.assertEqual(snapshot["finalActions"], [])
        command = {"kind": "register", **snapshot["registrationActions"][0], "documentKey": snapshot["documentKey"]}
        result = self.page.evaluate("command => window.__request({type:'application-commands',commands:[command]})", command)
        self.assertEqual(result[0]["status"], "blocked")
        self.start()
        self.execute()
        self.page.wait_for_url("**/register")

    def test_mismatched_passwords_and_captcha_prevent_account_creation(self):
        self.install('<textarea name="g-recaptcha-response" hidden></textarea>')
        self.page.locator("#first").fill("Test")
        self.page.locator("#email").fill(self.profile["email"])
        self.page.locator("#password").fill("ExampleSecret123!")
        self.page.locator("#confirm").fill("DifferentSecret123!")
        self.assertEqual(self.snapshot()["registrationActions"], [])
        self.page.locator("#confirm").fill("ExampleSecret123!")
        self.assertEqual(self.snapshot()["registrationActions"], [])
        self.page.evaluate("document.querySelector('[name=g-recaptcha-response]').value='test-verification-token'")
        snapshot = self.snapshot()
        command = {"kind": "register", **snapshot["registrationActions"][0], "documentKey": snapshot["documentKey"], "authorized": True}
        statuses = self.page.evaluate("async command => {const first=await window.__request({type:'application-commands',commands:[command]});const second=await window.__request({type:'application-commands',commands:[command]});return [first[0].status,second[0].status]}", command)
        self.assertEqual(statuses, ["clicked", "already_attempted"])
        self.assertEqual(self.page.evaluate("window.created"), 1)


if __name__ == "__main__":
    unittest.main()
