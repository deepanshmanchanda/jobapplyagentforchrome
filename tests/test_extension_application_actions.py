"""Exercise application uploads, submission, and saved-browser login in Chromium."""

import base64
import json
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


CONTENT = (Path(__file__).parents[1] / "chrome_extension" / "content.js").read_text(encoding="utf-8")
RUNTIME = """() => {
  window.__listeners = [];
  window.__messages = [];
  window.chrome = { runtime: {
    getManifest: () => ({ version: window.__version || 'test-v6' }),
    sendMessage: (message) => { window.__messages.push(message); return Promise.resolve(); },
    onMessage: {
      addListener: (listener) => window.__listeners.push(listener),
      removeListener: (listener) => { window.__listeners = window.__listeners.filter(item => item !== listener); },
    },
  } };
  window.__request = (message) => new Promise(resolve => window.__listeners[0](message, {}, resolve));
}"""


class ExtensionApplicationActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def install(self, html):
        self.page.set_content(html)
        self.page.evaluate(RUNTIME)
        self.page.add_script_tag(content=CONTENT)

    def snapshot(self):
        return self.page.evaluate("() => window.__request({type: 'application-snapshot'})")

    def test_workday_add_reveals_row_and_cannot_replay(self):
        self.page.route('https://example.myworkdayjobs.com/**', lambda route: route.fulfill(body='<h1>Application</h1>', content_type='text/html'))
        self.page.goto('https://example.myworkdayjobs.com/apply')
        self.install('''<section data-automation-id="workExperienceSection">
            <h2>Work Experience</h2><div id="rows"></div>
            <button id="add" onclick="document.querySelector('#rows').innerHTML +=
            '&lt;label&gt;Company&lt;input id=workExperience-1--companyName&gt;&lt;/label&gt;'">Add</button>
            </section>''')
        snapshot = self.snapshot()
        section = snapshot['resumeSections'][0]
        self.assertEqual(section['kind'], 'employment')
        command = dict(kind='resume-add', **section['add'], section='employment', rowCount=0,
                       authorized=True, documentKey=snapshot['documentKey'])
        self.assertEqual(self.execute(command), 'clicked')
        updated = self.snapshot()
        self.assertEqual(updated['fields'][0]['resumeRow'], 'workExperience-1')
        self.assertEqual(self.execute(command), 'stale_document')

    def test_job_entry_link_with_blank_target_stays_in_current_tab(self):
        self.page.route('https://jobs.example.test/**', lambda route: route.fulfill(body='<h1>Application</h1>', content_type='text/html'))
        self.page.goto('https://jobs.example.test/listing')
        self.install('<a href="/apply" target="_blank">Apply for this job</a><a href="/apply">Apply for this job</a>')
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot['actions']), 1)
        popups = []
        self.page.on('popup', lambda popup: popups.append(popup))
        self.assertEqual(self.execute({'kind': 'click', **snapshot['actions'][0], 'documentKey': snapshot['documentKey']}), 'clicked')
        self.page.wait_for_url('**/apply')
        self.assertEqual(popups, [])

    def test_apply_link_survives_unrelated_newsletter_controls(self):
        self.page.route('https://jobs.example.test/**', lambda route: route.fulfill(body='<h1>Application</h1>', content_type='text/html'))
        self.page.goto('https://jobs.example.test/listing')
        self.install('<a href="/apply" target="_blank">Apply</a><form><label>Newsletter email<input type=email></label></form>')
        snapshot = self.snapshot()
        self.assertEqual([a['label'] for a in snapshot['actions']], ['Apply'])
        self.assertEqual(self.execute(dict(kind='click', **snapshot['actions'][0], documentKey=snapshot['documentKey'])), 'clicked')
        self.page.wait_for_url('**/apply')

    def test_entry_button_uses_visible_label_not_internal_value(self):
        self.install('<button value="job-123" title="Begin recruiting process">Apply online</button>')
        self.assertEqual([a['label'] for a in self.snapshot()['actions']], ['Apply online'])

    def test_entry_action_labels_include_common_employer_variants(self):
        self.install('<button>Apply</button>')
        for label in ['Apply', 'Apply for this position', 'Apply for the role', 'Start your application']:
            self.page.locator('button').evaluate('(element, label) => element.textContent = label', label)
            self.assertEqual([a['label'] for a in self.snapshot()['actions']], [label])

    def test_plain_apply_cannot_submit_a_form_as_navigation(self):
        self.install('<form><label>Name<input value="Candidate"></label><button>Apply</button></form>')
        self.assertEqual(self.snapshot()['actions'], [])

    def test_apply_now_inside_a_form_is_not_entry_navigation(self):
        self.install('<form><label>Email<input type=email></label><button>Apply now</button></form>')
        self.assertEqual(self.snapshot()['actions'], [])

    def test_only_workday_current_employment_checkbox_can_be_set(self):
        self.page.route('https://example.myworkdayjobs.com/**', lambda route: route.fulfill(body='<html></html>', content_type='text/html'))
        self.page.goto('https://example.myworkdayjobs.com/apply')
        self.install('<label for="workExperience-5--currentlyWorkHere">I currently work here</label><input type=checkbox id="workExperience-5--currentlyWorkHere"><label for=terms>I agree to terms</label><input type=checkbox id=terms>')
        snapshot = self.snapshot()
        command = {'kind':'check', 'selector':'#workExperience-5--currentlyWorkHere', 'label':'I currently work here', 'value':'true', 'documentKey':snapshot['documentKey']}
        self.assertEqual(self.execute(command), 'applied')
        self.assertTrue(self.page.locator('[id="workExperience-5--currentlyWorkHere"]').is_checked())
        self.assertEqual(self.execute({**command, 'selector':'#terms', 'label':'I agree to terms'}), 'blocked')
        self.assertFalse(self.page.locator('#terms').is_checked())

    def execute(self, command):
        return self.page.evaluate(
            "command => window.__request({type: 'application-commands', commands: [command]})", command
        )[0]["status"]

    def upload_command(self, snapshot, field=None, **changes):
        field = field or snapshot["fields"][0]
        command = {
            "kind": "upload", "selector": field["selector"], "label": field["label"],
            "documentKey": snapshot["documentKey"],
            "file": {"name": "candidate.pdf", "mimeType": "application/pdf",
                     "dataBase64": base64.b64encode(b"%PDF-1.4 candidate resume").decode()},
        }
        command.update(changes)
        return command

    def test_hidden_react_uploads_get_distinct_labels_and_stable_selectors(self):
        self.install("""
          <section><div>Resume / CV <input type=file accept='.pdf,.docx' style='display:none' required></div></section>
          <section><div>Cover letter <input type=file style='display:none'></div></section>
          <input type=file disabled aria-label='Disabled resume'>
        """)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["bridgeProtocol"], 8)
        self.assertEqual([field["fileRole"] for field in snapshot["fields"]], ["resume", "cover_letter"])
        first, second = snapshot["fields"]
        self.assertNotEqual(first["selector"], second["selector"])
        self.assertTrue(first["required"])
        self.assertEqual(first["accept"], ".pdf,.docx")
        self.assertEqual(first["files"], [])
        self.assertEqual(snapshot["documentKey"], self.snapshot()["documentKey"])

    def test_resume_upload_dispatches_real_file_and_does_not_overwrite(self):
        self.install("<label for=resume>Resume</label><input id=resume type=file accept='.pdf' style='display:none'>")
        self.page.evaluate("""() => {
          window.__events = [];
          for (const kind of ['input', 'change']) document.querySelector('#resume').addEventListener(kind, () => window.__events.push(kind));
        }""")
        command = self.upload_command(self.snapshot())
        self.assertEqual(self.execute(command), "uploaded")
        actual = self.page.evaluate("""async () => ({
          text: await document.querySelector('#resume').files[0].text(),
          type: document.querySelector('#resume').files[0].type,
          events: window.__events,
        })""")
        self.assertEqual(actual, {"text": "%PDF-1.4 candidate resume", "type": "application/pdf", "events": ["input", "change"]})
        command["file"]["name"] = "replacement.pdf"
        self.assertEqual(self.execute(command), "already_attached")
        updated = self.snapshot()
        self.assertEqual(updated["fields"][0]["files"], ["candidate.pdf"])
        self.assertEqual(updated["fields"][0]["value"], "candidate.pdf")
        self.assertNotIn("fakepath", json.dumps(updated))
        self.assertNotIn("dataBase64", json.dumps(updated))

    def test_upload_requires_current_resume_control_and_valid_file(self):
        self.install("""
          <label for=resume>Resume</label><input id=resume type=file accept='.docx'>
          <label for=letter>Cover letter</label><input id=letter type=file>
        """)
        snapshot = self.snapshot()
        command = self.upload_command(snapshot)
        self.assertEqual(self.execute(command), "file_type_not_accepted")
        command["file"]["name"] = "../resume.docx"
        self.assertEqual(self.execute(command), "invalid_file")
        command["file"]["name"] = "resume.docx"
        command["file"]["dataBase64"] = "not-base64!"
        self.assertEqual(self.execute(command), "invalid_file")
        self.assertEqual(self.execute(self.upload_command(snapshot, snapshot["fields"][1])), "blocked")
        self.assertEqual(self.execute(self.upload_command(snapshot, documentKey="old-page")), "stale_document")
        self.assertEqual(self.execute(self.upload_command(snapshot, documentKey="")), "blocked")
        self.assertEqual(self.page.locator("#resume").evaluate("element => element.files.length"), 0)

    def test_submission_is_separate_authorized_and_deduplicated_across_reinjection(self):
        self.install("<button id=final>Submit application</button><button>Continue</button>")
        self.page.evaluate("() => { window.__submitted = 0; document.querySelector('#final').onclick = () => ++window.__submitted; }")
        snapshot = self.snapshot()
        self.assertEqual([action["label"] for action in snapshot["actions"]], ["Continue"])
        self.assertEqual([action["label"] for action in snapshot["finalActions"]], ["Submit application"])
        command = {"kind": "submit", **snapshot["finalActions"][0], "documentKey": snapshot["documentKey"]}
        self.assertEqual(self.execute(command), "blocked")
        self.assertEqual(self.execute({**command, "kind": "click"}), "blocked")
        self.assertEqual(self.execute({**command, "authorized": True, "documentKey": ""}), "blocked")
        self.assertEqual(self.execute({**command, "authorized": True, "documentKey": "stale"}), "stale_document")
        command["authorized"] = True
        self.assertEqual(self.execute(command), "submission_clicked")
        self.assertFalse(self.snapshot()["submissionConfirmation"])
        self.assertEqual(self.execute(command), "already_submitted")
        self.page.evaluate("() => window.__version = 'updated-build'")
        self.page.add_script_tag(content=CONTENT)
        self.assertEqual(self.execute(command), "already_submitted")
        self.assertEqual(self.page.evaluate("() => window.__submitted"), 1)

    def test_confirmation_requires_a_visible_success_statement(self):
        self.install("""
          <p>Once your application has been submitted, you will receive an email.</p>
          <p style='display:none'>Your application has been submitted.</p>
          <button>Submit application</button>
        """)
        self.assertFalse(self.snapshot()["submissionConfirmation"])
        self.page.locator("body").evaluate("element => element.innerHTML = '<h1>Your application has been received.</h1>'")
        self.assertTrue(self.snapshot()["submissionConfirmation"])

    def test_unchecked_required_checkbox_and_invalid_form_block_submission(self):
        self.install("""
          <form onsubmit='return false'><label for=terms>I agree to the application terms</label>
            <input id=terms type=checkbox required><button id=final>Submit application</button>
          </form>
        """)
        self.page.evaluate("() => { window.__submitted = 0; document.querySelector('#final').onclick = () => ++window.__submitted; }")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["fields"][0]["value"], "")
        self.assertTrue(snapshot["fields"][0]["invalid"])
        command = {"kind": "submit", **snapshot["finalActions"][0], "documentKey": snapshot["documentKey"], "authorized": True}
        self.assertEqual(self.execute(command), "validation_failed")
        self.assertEqual(self.page.evaluate("() => window.__submitted"), 0)
        self.page.locator("#terms").check()
        self.assertEqual(self.execute(command), "submission_clicked")
        self.assertEqual(self.page.evaluate("() => window.__submitted"), 1)

    def test_login_uses_saved_browser_values_without_exposing_credentials(self):
        self.install("""
          <form onsubmit='return false'>
            <label for=email>Email address</label><input id=email type=email autocomplete=username required>
            <label for=password>Password</label><input id=password type=password required>
            <button id=login>Sign in</button>
          </form>
        """)
        empty = self.snapshot()
        self.assertTrue(empty["accessPending"])
        self.assertFalse(empty["accessReady"])
        self.page.locator("#email").fill("login@example.test")
        self.page.locator("#password").fill("saved-browser-secret")
        self.page.evaluate("() => { window.__loggedIn = 0; document.querySelector('#login').onclick = () => ++window.__loggedIn; }")
        ready = self.snapshot()
        self.assertFalse(ready["accessPending"])
        self.assertTrue(ready["accessReady"])
        self.assertEqual(ready["fields"], [])
        self.assertNotIn("login@example.test", json.dumps(ready))
        self.assertNotIn("saved-browser-secret", json.dumps(ready))
        command = {"kind": "authenticate", **ready["accessActions"][0], "documentKey": ready["documentKey"]}
        self.assertEqual(self.execute(command), "blocked")
        self.assertEqual(self.execute({**command, "authorized": True}), "clicked")
        self.assertEqual(self.execute({**command, "authorized": True}), "already_attempted")
        self.assertEqual(self.page.evaluate("() => window.__loggedIn"), 1)

    def test_optional_sidebar_login_does_not_interrupt_application(self):
        self.install("""
          <form><label for=name>First name</label><input id=name required><button>Continue</button></form>
          <aside><form>
            <label for=email>Email</label><input id=email type=email autocomplete=username required>
            <label for=password>Password</label><input id=password type=password required>
            <button>Log in</button>
          </form></aside>
        """)
        snapshot = self.snapshot()
        self.assertFalse(snapshot["accessPending"])
        self.assertFalse(snapshot["accessReady"])
        self.assertEqual(snapshot["accessActions"], [])
        self.assertEqual([field["label"] for field in snapshot["fields"]], ["First name"])

    def test_authenticate_is_rejected_if_browser_credentials_disappear(self):
        self.install("""
          <form><input id=email type=email aria-label=Email value='login@example.test'>
          <input id=secret type=password value='browser-value'><button>Log in</button></form>
        """)
        snapshot = self.snapshot()
        command = {"kind": "authenticate", **snapshot["accessActions"][0],
                   "documentKey": snapshot["documentKey"], "authorized": True}
        self.page.locator("#secret").fill("")
        self.assertEqual(self.execute(command), "stale_document")

    def test_owned_degree_options_can_be_selected_and_menu_closes(self):
        self.page.route('https://example.myworkdayjobs.com/**', lambda route: route.fulfill(body='<h1>Application</h1>', content_type='text/html'))
        self.page.goto('https://example.myworkdayjobs.com/apply')
        self.install('''<section data-automation-id="educationSection"><h2>Education</h2>
          <button id="education-1--degree" aria-label="Degree" aria-haspopup="listbox"
            aria-controls="degrees" aria-expanded="true">Select one</button>
          <div id="degrees" role="listbox"><div role="option">Technical Diploma</div></div></section>''')
        self.page.evaluate('''() => {
          const field = document.querySelector('button');
          document.querySelector('[role=option]').onclick = () => {
            field.textContent = 'Technical Diploma'; field.setAttribute('aria-expanded', 'false');
            document.querySelector('[role=listbox]').hidden = true;
          };
          field.addEventListener('keydown', event => { if (event.key === 'Escape') window.escaped = true; });
        }''')
        snapshot = self.snapshot()
        field = snapshot['fields'][0]
        self.assertEqual(field['options'], ['Technical Diploma'])
        self.assertEqual(self.execute(dict(kind='resume-select', selector=field['selector'], label=field['label'],
            value='technical diploma', aliases=['technical diploma'], documentKey=snapshot['documentKey'])), 'applied')
        self.assertTrue(self.page.evaluate('window.escaped'))
        self.assertEqual(self.snapshot()['fields'][0]['value'], 'Technical Diploma')

    def test_workday_date_commits_through_focusout(self):
        self.page.route('https://example.myworkdayjobs.com/**', lambda route: route.fulfill(body='<h1>Application</h1>', content_type='text/html'))
        self.page.goto('https://example.myworkdayjobs.com/apply')
        self.install('''<label for="workExperience-1--startDate-dateSectionMonth-input">Month</label>
            <input id="workExperience-1--startDate-dateSectionMonth-input" aria-invalid="true">''')
        self.page.evaluate('''() => {
            const input = document.querySelector('input');
            input.addEventListener('focus', () => { window.dateFocused = true; });
            document.addEventListener('focusout', event => {
                if (event.target === input && window.dateFocused && input.value === '03') {
                    window.committedDate = input.value;
                    input.setAttribute('aria-invalid', 'false');
                }
            });
        }''')
        snapshot = self.snapshot()
        field = snapshot['fields'][0]
        self.assertEqual(self.execute(dict(kind='fill', selector=field['selector'], label=field['label'],
                                           value='03', documentKey=snapshot['documentKey'])), 'applied')
        self.assertEqual(self.page.evaluate('window.committedDate'), '03')
        self.assertFalse(self.snapshot()['fields'][0]['invalid'])

    def test_spoofed_command_label_cannot_fill_a_sensitive_field(self):
        self.install("<label for=visa>Visa sponsorship</label><input id=visa>")
        self.assertEqual(self.execute({"kind": "fill", "selector": "#visa", "label": "First name", "value": "Yes"}), "blocked")
        self.assertEqual(self.page.locator("#visa").input_value(), "")


if __name__ == "__main__":
    unittest.main()
