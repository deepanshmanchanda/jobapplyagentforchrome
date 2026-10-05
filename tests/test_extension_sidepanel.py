"""Regression coverage for the Chrome extension's in-page side-panel UX.

These tests deliberately run the panel as an ordinary extension page with a
small fake ``chrome.runtime``.  That gives us repeatable coverage of the
candidate-facing control flow without needing a signed Chrome installation or
an employer application page.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


EXTENSION = Path(__file__).parents[1] / "chrome_extension"


class ExtensionSidePanelTests(unittest.TestCase):
    def test_manifest_makes_the_side_panel_the_primary_extension_surface(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "0.9.0")
        self.assertIn("sidePanel", manifest["permissions"])
        self.assertGreaterEqual(int(manifest["minimum_chrome_version"]), 114)
        self.assertEqual(manifest["side_panel"]["default_path"], "sidepanel.html")
        self.assertNotIn("default_popup", manifest["action"])
        self.assertIn("http://127.0.0.1:8765/*", manifest["host_permissions"])
        self.assertIn("http://*/*", manifest["host_permissions"])
        self.assertIn("https://*/*", manifest["host_permissions"])
        self.assertNotIn("optional_host_permissions", manifest)

    def test_panel_assets_are_external_csp_safe_and_do_not_control_tabs_directly(self):
        html = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")
        script = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")

        self.assertIn('src="sidepanel.js"', html)
        self.assertIn('href="sidepanel.css"', html)
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I))
        self.assertNotIn("fetch(", script)
        self.assertNotIn("chrome.tabs", script)
        self.assertIn('request("panel-status")', script)
        self.assertIn('request("panel-start-autopilot", {', script)
        self.assertIn('request("panel-stop-autopilot")', script)
        self.assertIn('id="agent-instruction"', html)
        self.assertIn('id="send-instruction"', html)
        self.assertIn('request("panel-instruction-autopilot"', script)

    def test_background_injects_during_the_action_click_then_opens_the_side_panel(self):
        script = (EXTENSION / "background.js").read_text(encoding="utf-8")

        self.assertIn("chrome.action?.onClicked?.addListener", script)
        self.assertIn("injection = chrome.scripting.executeScript", script)
        self.assertIn("const panelOpening = chrome.sidePanel.open({ tabId: tab.id })", script)
        for message in ("panel-status", "panel-start-autopilot", "panel-stop-autopilot", "panel-instruction-autopilot"):
            self.assertIn(repr(message), script)
        # The panel button must bind the explicit active tab before it starts;
        # it must not silently reuse a different tab selected earlier.
        self.assertIn("async function startAutopilotForActiveTab(", script)
        self.assertIn("await sendSnapshot(tab, true, true);", script)
        self.assertIn("return true;", script)

    def test_action_click_injects_the_current_application_before_opening_the_panel(self):
        """The toolbar click uses Chrome's activeTab grant before panel Start."""
        worker_script = (EXTENSION / "background.js").read_text(encoding="utf-8")
        fake_worker = """
            (() => {
              const actionListeners = [];
              const runtimeListeners = [];
              const injections = [];
              const panelOpens = [];
              const stored = {};
              const listeners = () => ({ addListener() {} });
              const storage = {
                async get(keys) {
                  const names = Array.isArray(keys) ? keys : [keys];
                  return Object.fromEntries(names.map((name) => [name, stored[name]]));
                },
                async set(values) { Object.assign(stored, values); },
                async remove(keys) { for (const key of (Array.isArray(keys) ? keys : [keys])) delete stored[key]; },
              };
              globalThis.chrome = {
                action: { onClicked: { addListener(listener) { actionListeners.push(listener); } } },
                sidePanel: { open: async (options) => { panelOpens.push(options); } },
                storage: { session: storage, local: storage },
                runtime: { onMessage: { addListener(listener) { runtimeListeners.push(listener); } } },
                scripting: { executeScript: async (options) => { injections.push(options); } },
                tabs: {
                  query: async () => [], get: async () => ({}), sendMessage: async () => ({}),
                  onUpdated: listeners(), onCreated: listeners(), onRemoved: listeners(), remove: async () => {},
                },
                alarms: { create() {}, onAlarm: listeners() },
              };
              globalThis.__actionWorkerTest = { actionListeners, injections, panelOpens };
            })();
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<!doctype html><title>action worker harness</title>")
            page.evaluate(fake_worker)
            page.add_script_tag(content=worker_script)
            result = page.evaluate("""async () => {
                await window.__actionWorkerTest.actionListeners[0]({
                  id: 77, url: 'https://smartapply.indeed.com/job/example'
                });
                return window.__actionWorkerTest;
            }""")
            browser.close()
        self.assertEqual(result["injections"][0]["target"]["tabId"], 77)
        self.assertEqual(result["injections"][0]["files"], ["content.js"])
        self.assertEqual(result["panelOpens"], [{"tabId": 77}])

    def test_side_panel_renders_live_status_and_start_stop_messages(self):
        """A visible side panel can start, display progress, and stop a run."""
        sidepanel = EXTENSION / "sidepanel.html"
        fake_chrome = """
            (() => {
              const messages = [];
              let paired = false;
              let running = false;
              const panel = () => ({
                paired,
                message: "Open an application page and choose Start Autopilot.",
                tab: paired ? { id: 77, title: "Example Data Analyst application", url: "https://jobs.example.test/apply" } : null,
                snapshot: paired ? { step: "Personal details", fieldCount: 6, errors: [] } : null,
                autopilot: paired
                  ? { state: running ? "running" : "off", reason: running ? "Queued 6 saved safe fields" : "Stopped", steps: running ? 1 : 0, maxSteps: 25 }
                  : { state: "off", reason: "Open an application page to begin." },
              });
              const respond = (message) => {
                messages.push(structuredClone(message));
                if (message.type === "panel-start-autopilot") { paired = true; running = true; }
                if (message.type === "panel-stop-autopilot") { running = false; }
                if (message.type === "panel-instruction-autopilot") { running = true; }
                return {
                  ok: true,
                  message: message.type === "panel-instruction-autopilot"
                    ? "Instruction accepted. Rechecking the current page." : "",
                  panel: panel()
                };
              };
              globalThis.__panelMessages = messages;
              globalThis.chrome = {
                storage: { local: {
                  async get(key) { return { [key]: JSON.parse(localStorage.getItem(key) || 'null') }; },
                  async set(values) {
                    for (const [key, value] of Object.entries(values)) localStorage.setItem(key, JSON.stringify(value));
                  },
                } },
                runtime: {
                  sendMessage(message, callback) {
                    const response = respond(message);
                    if (typeof callback === "function") {
                      queueMicrotask(() => callback(response));
                      return undefined;
                    }
                    return Promise.resolve(response);
                  },
                },
              };
            })();
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script(script=fake_chrome)
            page.goto(sidepanel.as_uri())
            page.wait_for_function(
                "() => window.__panelMessages.some((item) => item.type === 'panel-status')"
            )

            # It is a real DOM flow, not merely a source-code string check.
            self.assertEqual(page.locator("#status-title").inner_text(), "Ready when you are")
            self.assertEqual(page.locator("#start").inner_text(), "Start Autopilot")
            self.assertTrue(page.locator("#auto-submit").is_checked())
            page.locator("#start").click()
            page.wait_for_function(
                "() => window.__panelMessages.some((item) => item.type === 'panel-start-autopilot')"
            )
            self.assertEqual(page.locator("#page-name").inner_text(), "Example Data Analyst application")
            self.assertEqual(page.locator("#status-title").inner_text(), "Applying in this tab")
            started = page.evaluate("() => window.__panelMessages.find((item) => item.type === 'panel-start-autopilot')")
            self.assertEqual(started, {"type": "panel-start-autopilot", "maxSteps": 25, "autoSubmit": True, "autoRegister": True})
            self.assertIn("1 of 25", page.locator("#progress").inner_text())
            self.assertFalse(page.locator("#stop").is_hidden())

            page.locator("#agent-instruction").fill("Continue")
            page.locator("#send-instruction").click()
            page.wait_for_function(
                "() => window.__panelMessages.some((item) => item.type === 'panel-instruction-autopilot')"
            )
            instruction = page.evaluate(
                "() => window.__panelMessages.find((item) => item.type === 'panel-instruction-autopilot')"
            )
            self.assertEqual(instruction["instruction"], "Continue")
            self.assertTrue(instruction["remember"])
            self.assertIn("Instruction accepted", page.locator("#instruction-feedback").inner_text())

            page.locator("#stop").click()
            page.wait_for_function(
                "() => window.__panelMessages.some((item) => item.type === 'panel-stop-autopilot')"
            )
            self.assertEqual(page.locator("#status-title").inner_text(), "Ready when you are")

            # A review preference survives reopening the panel and reaches
            # the next run without an additional confirmation dialog.
            page.locator("#auto-submit").uncheck()
            page.locator("#auto-register").uncheck()
            page.wait_for_function("() => localStorage.getItem('applicationAutopilotAutoRegister') === 'false'")
            page.wait_for_function("() => localStorage.getItem('applicationAutopilotAutoSubmit') === 'false'")
            page.reload()
            page.wait_for_function("() => !document.querySelector('#auto-submit').disabled")
            self.assertFalse(page.locator("#auto-submit").is_checked())
            page.locator("#start").click()
            page.wait_for_function("() => window.__panelMessages.some((item) => item.type === 'panel-start-autopilot')")
            restarted = page.evaluate("() => window.__panelMessages.find((item) => item.type === 'panel-start-autopilot')")
            self.assertFalse(restarted["autoSubmit"])
            self.assertFalse(restarted["autoRegister"])

            for state, title in (
                ("waiting_for_submission", "Waiting for submission confirmation"),
                ("needs_user_input", "A missing answer needs your attention"),
                ("action_failed", "Application step needs attention"),
                ("submission_unconfirmed", "Submission not confirmed"),
                ("submitted", "Application submitted"),
            ):
                page.evaluate("state => render({ paired: true, autopilot: { state } })", state)
                self.assertEqual(page.locator("#status-title").inner_text(), title)
            self.assertTrue(page.locator("#stop").is_hidden())
            browser.close()

    def test_background_start_binds_the_active_tab_and_keeps_profile_data_local(self):
        """Exercise the panel message through the actual worker code.

        The worker is evaluated against a small MV3-shaped Chrome API.  This
        catches a surprisingly easy regression: a global side panel can stay
        open while the user changes tabs, so Start must use the *current*
        active tab rather than an old ``pairedTabId``.  It also proves the
        extension sends the run limit and submission preference to the
        loopback bridge without copying profile data into the panel.
        """
        worker_script = (EXTENSION / "background.js").read_text(encoding="utf-8")
        fake_worker = """
            (() => {
              const stored = { pairedTabId: 21 };
              const runtimeListeners = [];
              const fetches = [];
              let running = true;
              const listeners = () => ({ addListener() {} });
              const storage = {
                async get(keys) {
                  if (keys == null) return { ...stored };
                  const names = Array.isArray(keys) ? keys : [keys];
                  return Object.fromEntries(names.map((name) => [name, stored[name]]));
                },
                async set(values) { Object.assign(stored, values); },
                async remove(keys) { for (const key of (Array.isArray(keys) ? keys : [keys])) delete stored[key]; },
              };
              const response = (payload) => ({ ok: true, status: 200, json: async () => payload });
              globalThis.fetch = async (url, options = {}) => {
                fetches.push({ url, options: { method: options.method || "GET", body: options.body || "" } });
                if (url.endsWith("/v1/tabs/snapshot")) return response({ ok: true });
                if (url.endsWith("/v1/commands/77")) return response({ commands: [] });
                if (url.endsWith("/v1/tabs")) return response({ tabs: [{ tabId: "77", activeStep: "Personal details", fields: [], errors: [] }] });
                if (url.endsWith("/v1/autopilot/77/instruction")) return response({
                  message: "Instruction accepted. Rechecking the current page.",
                  status: { state: "running", reason: "Queued safe fields", steps: 1, maxSteps: 25 },
                });
                if (url.endsWith("/v1/autopilot/77")) {
                  if (options.method === "DELETE") running = false;
                  return response({ status: {
                    state: running ? "running" : "off",
                    reason: running ? "Queued safe fields" : "Stopped",
                    steps: running ? 1 : 0,
                    maxSteps: 25,
                  } });
                }
                throw new Error(`Unexpected local bridge route: ${url}`);
              };
              globalThis.chrome = {
                sidePanel: { setPanelBehavior: () => Promise.resolve() },
                storage: { session: storage, local: storage },
                runtime: { onMessage: { addListener(listener) { runtimeListeners.push(listener); } } },
                scripting: { executeScript: async () => {} },
                tabs: {
                  query: async () => [{ id: 77, title: "Current application", url: "https://jobs.example.test/apply" }],
                  get: async (id) => ({ id, title: id === 77 ? "Current application" : "Old application", url: "https://jobs.example.test/apply" }),
                  sendMessage: async () => ({
                    url: "https://jobs.example.test/apply", title: "Current application", body: "Application",
                    fields: [], actions: [], activeStep: "Personal details",
                  }),
                  onUpdated: listeners(), onCreated: listeners(), remove: async () => {},
                },
                alarms: { create() {}, onAlarm: listeners() },
              };
              globalThis.__workerTest = {
                fetches, stored,
                invoke(type, payload = {}) {
                  return new Promise((resolve, reject) => {
                    const listener = runtimeListeners[0];
                    const keepChannel = listener({ type, ...payload }, {}, (result) => resolve({ keepChannel, result }));
                    setTimeout(() => reject(new Error(`Timed out handling ${type}`)), 1000);
                  });
                },
              };
            })();
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<!doctype html><title>worker harness</title>")
            page.evaluate(fake_worker)
            page.add_script_tag(content=worker_script)

            started = page.evaluate("() => window.__workerTest.invoke('panel-start-autopilot')")
            self.assertTrue(started["keepChannel"])
            self.assertTrue(started["result"]["ok"])
            self.assertEqual(started["result"]["panel"]["tab"]["id"], 77)
            self.assertEqual(page.evaluate("() => window.__workerTest.stored.pairedTabId"), 77)
            autopilot_post = page.evaluate("""() => window.__workerTest.fetches.find((call) =>
                call.url.endsWith('/v1/autopilot/77') && call.options.method === 'POST'
            )""")
            self.assertEqual(json.loads(autopilot_post["options"]["body"]), {"maxSteps": 25, "autoSubmit": True, "autoRegister": True})
            snapshot_post = page.evaluate("""() => window.__workerTest.fetches.find((call) =>
                call.url.endsWith('/v1/tabs/snapshot')
            )""")
            self.assertEqual(json.loads(snapshot_post["options"]["body"])["bridgeProtocol"], 8)

            page.evaluate("() => window.__workerTest.invoke('panel-start-autopilot', {autoSubmit: false})")
            review_post = page.evaluate("""() => window.__workerTest.fetches.filter((call) =>
                call.url.endsWith('/v1/autopilot/77') && call.options.method === 'POST'
            ).at(-1)""")
            self.assertEqual(json.loads(review_post["options"]["body"]), {"maxSteps": 25, "autoSubmit": False, "autoRegister": True})

            instructed = page.evaluate("""() => window.__workerTest.invoke('panel-instruction-autopilot', {
                instruction: 'Answer Professional headline: Data analyst', remember: true
            })""")
            self.assertTrue(instructed["result"]["ok"])
            instruction_post = page.evaluate("""() => window.__workerTest.fetches.find((call) =>
                call.url.endsWith('/v1/autopilot/77/instruction') && call.options.method === 'POST'
            )""")
            self.assertEqual(json.loads(instruction_post["options"]["body"]), {
                "instruction": "Answer Professional headline: Data analyst", "remember": True,
            })

            stopped = page.evaluate("() => window.__workerTest.invoke('panel-stop-autopilot')")
            self.assertTrue(stopped["result"]["ok"])
            self.assertEqual(stopped["result"]["panel"]["autopilot"]["state"], "off")
            browser.close()

    def test_background_can_start_two_distinct_tabs_without_stopping_the_first_run(self):
        """Each explicit Start owns only the current tab, even when another run exists.

        This exercises the worker rather than merely inspecting its source.  In
        particular, it guards against the former single ``pairedTabId`` design
        where starting tab 88 deleted tab 77's Autopilot run.
        """
        worker_script = (EXTENSION / "background.js").read_text(encoding="utf-8")
        fake_worker = """
            (() => {
              const stored = {};
              const runtimeListeners = [];
              const fetches = [];
              let activeTabId = 77;
              const tabs = {
                77: { id: 77, title: "First application", url: "https://jobs.example.test/first" },
                88: { id: 88, title: "Second application", url: "https://jobs.example.test/second" },
              };
              const runs = {};
              const listeners = () => ({ addListener() {} });
              const storage = {
                async get(keys) {
                  if (keys == null) return { ...stored };
                  const names = Array.isArray(keys) ? keys : [keys];
                  return Object.fromEntries(names.map((name) => [name, stored[name]]));
                },
                async set(values) { Object.assign(stored, values); },
                async remove(keys) { for (const key of (Array.isArray(keys) ? keys : [keys])) delete stored[key]; },
              };
              const response = (payload) => ({ ok: true, status: 200, json: async () => payload });
              globalThis.fetch = async (url, options = {}) => {
                const method = options.method || "GET";
                fetches.push({ url, options: { method, body: options.body || "" } });
                if (url.endsWith("/v1/tabs/snapshot")) return response({ ok: true });
                if (/\\/v1\\/commands\\/(77|88)$/.test(url)) return response({ commands: [] });
                if (url.endsWith("/v1/tabs")) {
                  return response({ tabs: Object.values(tabs).map((tab) => ({
                    tabId: String(tab.id), activeStep: "Personal details", fields: [], errors: [],
                  })) });
                }
                const match = url.match(/\\/v1\\/autopilot\\/(77|88)$/);
                if (match) {
                  const tabId = match[1];
                  if (method === "POST") runs[tabId] = true;
                  if (method === "DELETE") runs[tabId] = false;
                  return response({ status: {
                    state: runs[tabId] ? "running" : "off",
                    reason: runs[tabId] ? "Queued safe fields" : "Stopped",
                    steps: 0, maxSteps: 25,
                  } });
                }
                throw new Error(`Unexpected local bridge route: ${url}`);
              };
              globalThis.chrome = {
                sidePanel: { setPanelBehavior: () => Promise.resolve() },
                storage: { session: storage, local: storage },
                runtime: { onMessage: { addListener(listener) { runtimeListeners.push(listener); } } },
                scripting: { executeScript: async () => {} },
                tabs: {
                  query: async () => [tabs[activeTabId]],
                  get: async (id) => tabs[id],
                  sendMessage: async (id) => ({
                    url: tabs[id].url, title: tabs[id].title, body: "Application",
                    fields: [], actions: [], activeStep: "Personal details",
                  }),
                  onUpdated: listeners(), onCreated: listeners(), onRemoved: listeners(), remove: async () => {},
                },
                alarms: { create() {}, onAlarm: listeners() },
              };
              globalThis.__multiTabWorkerTest = {
                fetches, stored, runs,
                setActive(tabId) { activeTabId = tabId; },
                invoke(type) {
                  return new Promise((resolve, reject) => {
                    const listener = runtimeListeners[0];
                    const keepChannel = listener({ type }, {}, (result) => resolve({ keepChannel, result }));
                    setTimeout(() => reject(new Error(`Timed out handling ${type}`)), 1000);
                  });
                },
              };
            })();
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<!doctype html><title>multi-tab worker harness</title>")
            page.evaluate(fake_worker)
            page.add_script_tag(content=worker_script)

            # Start both pages before either asynchronous storage write can
            # finish. This catches a lost-update race in the managed tab list.
            first, second = page.evaluate("""() => {
                const first = window.__multiTabWorkerTest.invoke('panel-start-autopilot');
                window.__multiTabWorkerTest.setActive(88);
                const second = window.__multiTabWorkerTest.invoke('panel-start-autopilot');
                return Promise.all([first, second]);
            }""")

            self.assertTrue(first["result"]["ok"])
            self.assertTrue(second["result"]["ok"])
            self.assertEqual(second["result"]["panel"]["tab"]["id"], 88)
            self.assertTrue(page.evaluate("() => window.__multiTabWorkerTest.runs['77']"))
            self.assertTrue(page.evaluate("() => window.__multiTabWorkerTest.runs['88']"))
            self.assertEqual(
                page.evaluate("() => window.__multiTabWorkerTest.stored.managedApplicationTabIds"),
                [77, 88],
            )

            autopilot_posts = page.evaluate("""() => window.__multiTabWorkerTest.fetches.filter((call) =>
                /\\/v1\\/autopilot\\/(77|88)$/.test(call.url) && call.options.method === 'POST'
            )""")
            self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in autopilot_posts], ["77", "88"])
            self.assertEqual(
                [json.loads(call["options"]["body"]) for call in autopilot_posts],
                [{"maxSteps": 25, "autoSubmit": True, "autoRegister": True}, {"maxSteps": 25, "autoSubmit": True, "autoRegister": True}],
            )
            self.assertFalse(page.evaluate("""() => window.__multiTabWorkerTest.fetches.some((call) =>
                call.url.endsWith('/v1/autopilot/77') && call.options.method === 'DELETE'
            )"""))
            browser.close()

    def test_command_retry_caches_only_receipts_and_does_not_repeat_submission(self):
        """A failed local acknowledgement must not replay a submitted form.

        Use local storage as the fallback, and make the fake content script
        echo its entire command to verify payloads cannot leak into storage.
        """
        worker_script = (EXTENSION / "background.js").read_text(encoding="utf-8")
        fake_worker = """
            (() => {
              const stored = {};
              const sent = [];
              const acknowledgements = [];
              let rejectAcknowledgement = true;
              let suppressActions = false;
              const commands = [
                { commandId: 'upload-1', kind: 'upload', selector: '#resume', fileBytes: 'PRIVATE_RESUME_BYTES' },
                { commandId: 'submit-1', kind: 'submit', selector: '#submit' },
                { commandId: 'auth-1', kind: 'authenticate', selector: '#login', password: 'TEST_SECRET' },
              ];
              const listeners = () => ({ addListener() {} });
              const storage = {
                async get(key) { return { [key]: stored[key] }; },
                async set(values) { Object.assign(stored, structuredClone(values)); },
                async remove(key) { delete stored[key]; },
              };
              const response = (payload, ok = true) => ({ ok, status: ok ? 200 : 503, json: async () => payload });
              globalThis.fetch = async (url, options = {}) => {
                if (url.endsWith('/v1/commands/77')) return response({ commands });
                if (url.endsWith('/v1/results')) {
                  acknowledgements.push(JSON.parse(options.body));
                  const accepted = !rejectAcknowledgement;
                  rejectAcknowledgement = false;
                  return response({}, accepted);
                }
                if (url.endsWith('/v1/tabs/snapshot')) return response({});
                throw new Error(`Unexpected request: ${url}`);
              };
              globalThis.chrome = {
                storage: { local: storage },
                runtime: { onMessage: listeners() },
                tabs: {
                  async sendMessage(tabId, message) {
                    if (message.type === 'application-snapshot') return { url: 'https://jobs.example.test/apply' };
                    sent.push({ tabId, guarded: Boolean(stored['applicationBridgeNavigationGuards:77']) });
                    const statuses = suppressActions
                      ? { submit: 'already_submitted', authenticate: 'already_attempted', upload: 'already_attached' }
                      : { submit: 'submission_clicked', authenticate: 'clicked', upload: 'uploaded' };
                    return message.commands.map((command) => ({
                      ...command, status: statuses[command.kind],
                    }));
                  },
                  async get(id) { return { id, url: 'https://jobs.example.test/apply' }; },
                  onUpdated: listeners(), onCreated: listeners(), onRemoved: listeners(),
                },
                alarms: { create() {}, onAlarm: listeners() },
              };
              globalThis.__receiptTest = { stored, sent, acknowledgements };
              globalThis.__suppressActions = () => { suppressActions = true; };
            })();
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<!doctype html><title>receipt retry harness</title>")
            page.evaluate(fake_worker)
            page.add_script_tag(content=worker_script)
            page.evaluate("() => pollManagedTab(77)")
            after_failure = page.evaluate("() => structuredClone(window.__receiptTest)")
            receipts = after_failure["stored"]["applicationBridgeCommandResults:77"]
            self.assertEqual(len(receipts), 3)
            self.assertEqual(receipts["submit-1"], {
                "commandId": "submit-1", "selector": "#submit", "status": "submission_clicked",
            })
            self.assertEqual(after_failure["sent"], [{"tabId": 77, "guarded": True}])
            serialized = json.dumps(after_failure)
            self.assertNotIn("PRIVATE_RESUME_BYTES", serialized)
            self.assertNotIn("TEST_SECRET", serialized)
            self.assertNotIn("fileBytes", serialized)
            self.assertNotIn("password", serialized)

            page.evaluate("() => pollManagedTab(77)")
            after_retry = page.evaluate("() => structuredClone(window.__receiptTest)")
            self.assertEqual(len(after_retry["sent"]), 1)
            self.assertEqual(len(after_retry["acknowledgements"]), 2)
            self.assertEqual(after_retry["acknowledgements"][0], after_retry["acknowledgements"][1])
            self.assertEqual(after_retry["stored"]["applicationBridgeCommandResults:77"], {})

            # A fresh command can also be refused by content-level replay
            # protection. No dispatched click means no lingering tab guard.
            page.evaluate("async () => { window.__suppressActions(); await pollManagedTab(77); }")
            after_suppression = page.evaluate("() => structuredClone(window.__receiptTest)")
            self.assertEqual(
                [receipt["status"] for receipt in after_suppression["acknowledgements"][-1]["results"]],
                ["already_attached", "already_submitted", "already_attempted"],
            )
            self.assertNotIn("applicationBridgeNavigationGuards:77", after_suppression["stored"])
            browser.close()

    def test_panel_javascript_parses_in_chromium(self):
        script = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            parse_error = page.evaluate(
                "source => { try { new Function(source); return ''; } catch (error) { return String(error); } }",
                script,
            )
            browser.close()
        self.assertEqual(parse_error, "")

    def test_content_script_can_be_injected_again_after_an_extension_reload(self):
        """A recovery Pair must not install duplicate listeners on an open ATS tab."""
        script = (EXTENSION / "content.js").read_text(encoding="utf-8")
        fake_chrome = """
            (() => {
              const listeners = [];
              globalThis.__contentListeners = listeners;
              globalThis.chrome = {
                runtime: {
                  getManifest: () => ({ version: "0.8.0" }),
                  sendMessage: () => Promise.resolve(),
                  onMessage: {
                    addListener(listener) { listeners.push(listener); },
                    removeListener(listener) {
                      const index = listeners.indexOf(listener);
                      if (index >= 0) listeners.splice(index, 1);
                    },
                  },
                },
              };
            })();
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("""
                <label for='first-name'>First name</label>
                <input id='first-name' type='text'>
                <label for='password'>Password</label>
                <input id='password' type='password'>
            """)
            page.evaluate(fake_chrome)
            page.add_script_tag(content=script)
            page.add_script_tag(content=script)
            self.assertEqual(page.evaluate("() => window.__contentListeners.length"), 1)
            snapshot = page.evaluate("""() => new Promise((resolve) =>
                window.__contentListeners[0]({ type: 'application-snapshot' }, {}, resolve)
            )""")
            browser.close()
        self.assertEqual([item["label"] for item in snapshot["fields"]], ["First name"])

    def test_content_script_learns_only_trusted_safe_manual_input(self):
        script = (EXTENSION / "content.js").read_text(encoding="utf-8")
        fake_chrome = """
            (() => {
              const listeners = [];
              const messages = [];
              globalThis.__contentMessages = messages;
              globalThis.chrome = {
                runtime: {
                  getManifest: () => ({ version: "0.8.0" }),
                  sendMessage(message) { messages.push(structuredClone(message)); return Promise.resolve(); },
                  onMessage: {
                    addListener(listener) { listeners.push(listener); },
                    removeListener(listener) {
                      const index = listeners.indexOf(listener);
                      if (index >= 0) listeners.splice(index, 1);
                    },
                  },
                },
              };
            })();
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("""
                <label for='headline'>Professional headline</label>
                <input id='headline' type='text'>
                <label for='password'>Password</label>
                <input id='password' type='password'>
                <label for='visa'>Visa sponsorship required</label>
                <input id='visa' type='text'>
            """)
            page.evaluate(fake_chrome)
            page.add_script_tag(content=script)
            page.locator("#headline").click()
            page.keyboard.type("Data analyst")
            page.wait_for_function(
                "() => window.__contentMessages.some((message) => message.type === 'application-manual-field')",
                timeout=3000,
            )
            learned = page.evaluate(
                "() => window.__contentMessages.filter((message) => message.type === 'application-manual-field')"
            )
            self.assertEqual(len(learned), 1)
            self.assertEqual(learned[0]["field"]["label"], "Professional headline")
            self.assertEqual(learned[0]["field"]["value"], "Data analyst")
            page.evaluate("""() => {
                const field = document.querySelector('#headline');
                field.value = 'Synthetic value';
                field.dispatchEvent(new Event('input', { bubbles: true }));
            }""")
            page.locator("#password").click()
            page.keyboard.type("not-a-real-password")
            page.locator("#visa").click()
            page.keyboard.type("No")
            page.wait_for_timeout(900)
            learned_after = page.evaluate(
                "() => window.__contentMessages.filter((message) => message.type === 'application-manual-field')"
            )
            browser.close()
        self.assertEqual(len(learned_after), 1)


if __name__ == "__main__":
    unittest.main()
