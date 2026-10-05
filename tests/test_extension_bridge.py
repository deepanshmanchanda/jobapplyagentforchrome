import json
import tempfile
import unittest
from pathlib import Path

from application_tracking import ApplicationTracker
from agent_field_memory import reusable_field_answer_key
from extension_bridge import ExtensionBridge, active_form_text, is_safe_click, load_saved_candidate_context
from job_assistant_rebuilt import (
    HSCNI_PERSONAL_DATA_EXPOSED_RAW_LABELS,
    HSCNI_PERSONAL_DATA_VISIBLE_SEQUENCE,
    extension_fill_plan,
    semantic_extension_fields,
)


class ExtensionBridgeTests(unittest.TestCase):
    def test_extension_only_autopilot_context_stays_in_local_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.sqlite3"
            tracker = ApplicationTracker(database)
            tracker.save_profile({"first_name": "Candidate", "email": "candidate@example.test"})
            tracker.save_answer("service", "Service example", "Reviewed answer", reviewed=True)
            tracker.save_answer("unreviewed", "Draft", "Do not use", reviewed=False)
            profile, answers = load_saved_candidate_context(database)
        self.assertEqual(profile["first_name"], "Candidate")
        self.assertEqual(answers, {"service": "Reviewed answer"})

    def test_snapshot_excludes_password_and_verification_fields(self):
        bridge = ExtensionBridge()
        tab = bridge.save_snapshot({
            "tabId": "42",
            "url": "https://example.test/application",
            "title": "Example application",
            "body": "Application page",
            "fields": [
                {"selector": "#email", "label": "Email address", "type": "email", "value": "candidate@example.test"},
                {"selector": "#password", "label": "Password", "type": "password", "value": "do-not-retain"},
                {"selector": "#otp", "label": "Verification code", "type": "text", "value": "123456"},
            ],
        })
        self.assertEqual([field["label"] for field in tab["fields"]], ["Email address"])

    def test_bridge_allows_only_safe_field_commands(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example",
            "fields": [{"selector": "#first-name", "label": "First name", "type": "text", "value": "", "options": []}],
        })
        queued = bridge.queue_commands("42", [{
            "kind": "fill", "selector": "#first-name", "label": "First name", "value": "Candidate",
        }])
        self.assertEqual(queued, 1)
        self.assertEqual(bridge.take_commands("42")[0]["selector"], "#first-name")
        with self.assertRaises(ValueError):
            bridge.queue_commands("42", [{
                "kind": "fill", "selector": "#password", "label": "Password", "value": "secret",
            }])
        with self.assertRaises(ValueError):
            bridge.queue_commands("42", [{
                "kind": "click", "selector": "#submit", "label": "Submit application",
            }])

    def test_bridge_rejects_direct_commands_for_legal_or_sensitive_fields(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example",
            "fields": [{
                "selector": "#right-to-work", "label": "Are you legally entitled to work here?",
                "type": "text", "value": "", "options": [],
            }],
        })
        with self.assertRaises(ValueError):
            bridge.queue_commands("42", [{
                "kind": "fill", "selector": "#right-to-work",
                "label": "Are you legally entitled to work here?", "value": "Yes",
            }])

    def test_manual_safe_input_is_learned_for_the_exact_site_and_field(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.sqlite3"
            bridge = ExtensionBridge(database)
            url = "https://jobs.example.test/application"
            bridge.save_snapshot({
                "tabId": "42", "url": url, "title": "Example", "body": "Application",
                "fields": [
                    {"selector": "#headline", "label": "Professional headline *", "type": "text", "value": "", "options": []},
                    {"selector": "#visa", "label": "Visa sponsorship required", "type": "text", "value": "", "options": []},
                    {"selector": "#ethnicity", "label": "Ethnicity", "type": "text", "value": "", "options": []},
                ],
            })
            bridge.remember_manual_field("42", {
                "selector": "#headline", "label": "Professional headline *", "type": "text", "value": "Data analyst",
            })
            bridge.remember_manual_field("42", {
                "selector": "#visa", "label": "Visa sponsorship required", "type": "text", "value": "No",
            })
            bridge.remember_manual_field("42", {
                "selector": "#ethnicity", "label": "Ethnicity", "type": "text", "value": "Example",
            })
            _, answers = load_saved_candidate_context(database)

            key = reusable_field_answer_key(url, "Professional headline", "text")
            self.assertEqual(answers[key], "Data analyst")
            self.assertNotIn(reusable_field_answer_key(url, "Visa sponsorship required", "text"), answers)
            self.assertNotIn(reusable_field_answer_key(url, "Ethnicity", "text"), answers)
            plan = extension_fill_plan({
                "tabId": "43", "url": url, "fields": [
                    {"selector": "#headline", "label": "Professional headline", "type": "text", "value": "", "options": []},
                ],
            }, {}, answers)
            self.assertEqual(plan["fields"][0]["proposed_value"], "Data analyst")
            self.assertEqual(plan["fields"][0]["source"], "Remembered manual answer")
            other_site_plan = extension_fill_plan({
                "tabId": "44", "url": "https://other.example.test/application", "fields": [
                    {"selector": "#headline", "label": "Professional headline", "type": "text", "value": "", "options": []},
                ],
            }, {}, answers)
            self.assertIsNone(other_site_plan["fields"][0]["proposed_value"])

    def test_panel_instruction_fills_one_safe_field_and_never_echoes_the_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.sqlite3"
            bridge = ExtensionBridge(database)
            url = "https://jobs.example.test/application"
            bridge.save_snapshot({
                "tabId": "42", "url": url, "title": "Example", "body": "Application",
                "documentKey": "profile-step",
                "fields": [{
                    "selector": "#headline", "label": "Professional headline", "type": "text",
                    "required": True, "value": "", "options": [],
                }],
            })
            bridge.start_autopilot("42", {}, {}, max_steps=2)
            result = bridge.apply_instruction(
                "42", "Answer Professional headline: Data analyst", remember=True,
            )
            commands = bridge.pending_commands("42")
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]["selector"], "#headline")
            self.assertEqual(commands[0]["value"], "Data analyst")
            self.assertIn("Understood: Professional headline: Data analyst", result["message"])
            self.assertNotIn("Data analyst", json.dumps(result["status"]))
            _, answers = load_saved_candidate_context(database)
            self.assertEqual(
                answers[reusable_field_answer_key(url, "Professional headline", "text")],
                "Data analyst",
            )

    def test_panel_instruction_refuses_a_sensitive_declaration(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example", "body": "Application",
            "fields": [{
                "selector": "#work-right", "label": "Are you legally entitled to work here?", "type": "text",
                "required": True, "value": "", "options": [],
            }],
        })
        bridge.start_autopilot("42", {}, {}, max_steps=2)
        with self.assertRaises(ValueError):
            bridge.apply_instruction("42", "Answer Are you legally entitled to work here?: Yes", remember=True)
        self.assertEqual(bridge.pending_commands("42"), [])

    def test_commands_persist_until_extension_acknowledges_them(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example",
            "fields": [{"selector": "#first-name", "label": "First name", "type": "text", "value": "", "options": []}],
        })
        bridge.queue_commands("42", [{
            "kind": "fill", "selector": "#first-name", "label": "First name", "value": "Candidate",
        }])
        pending = bridge.pending_commands("42")
        self.assertEqual(len(pending), 1)
        self.assertEqual(bridge.pending_commands("42")[0]["commandId"], pending[0]["commandId"])
        bridge.add_result({"tabId": "42", "results": [{
            "commandId": pending[0]["commandId"], "selector": "#first-name", "status": "applied",
        }]})
        self.assertEqual(bridge.pending_commands("42"), [])

    def test_autopilot_waits_for_a_snapshot_before_planning(self):
        bridge = ExtensionBridge()
        status = bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        self.assertEqual(status["state"], "waiting_for_snapshot")

    def test_stopping_autopilot_clears_queued_actions_for_that_tab(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example", "body": "Application",
            "fields": [{"selector": "#first", "label": "First name", "type": "text", "value": "", "options": []}],
        })
        bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        self.assertEqual(len(bridge.pending_commands("42")), 1)
        bridge.stop_autopilot("42")
        self.assertEqual(bridge.pending_commands("42"), [])

    def test_autopilot_fills_then_queues_only_a_non_final_progression(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example", "body": "Application",
            "fields": [{"selector": "#first-name", "label": "First name", "type": "text", "required": True, "value": "", "options": []}],
            "actions": [{"selector": "safe-action:next", "label": "Next"}],
        })
        bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        fill = bridge.pending_commands("42")[0]
        self.assertEqual(fill["kind"], "fill")
        bridge.add_result({"tabId": "42", "results": [{
            "commandId": fill["commandId"], "selector": "#first-name", "status": "applied",
        }]})
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example", "body": "Application",
            "fields": [{"selector": "#first-name", "label": "First name", "type": "text", "required": True, "value": "Candidate", "options": []}],
            "actions": [{"selector": "safe-action:next", "label": "Next"}],
        })
        next_step = bridge.pending_commands("42")[0]
        self.assertEqual(next_step["kind"], "click")
        self.assertEqual(next_step["label"], "Next")

    def test_autopilot_does_not_reclick_next_on_an_unchanged_snapshot(self):
        bridge = ExtensionBridge()
        snapshot = {
            "tabId": "42", "url": "https://example.test/application", "title": "Example", "body": "Application",
            "fields": [{"selector": "#first-name", "label": "First name", "type": "text", "required": True, "value": "Candidate", "options": []}],
            "actions": [{"selector": "safe-action:next:0", "label": "Next"}],
        }
        bridge.save_snapshot(snapshot)
        bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        click = bridge.pending_commands("42")[0]
        bridge.add_result({"tabId": "42", "results": [{
            "commandId": click["commandId"], "selector": click["selector"], "status": "clicked",
        }]})
        bridge.save_snapshot(snapshot)
        self.assertEqual(bridge.pending_commands("42"), [])
        self.assertEqual(bridge.autopilot_status("42")["state"], "waiting_for_navigation")

    def test_autopilot_does_not_treat_a_spinner_as_a_new_page_step(self):
        bridge = ExtensionBridge()
        first = {
            "tabId": "42", "url": "https://example.test/application", "title": "Example", "body": "Application",
            "documentKey": "page-a",
            "fields": [{"selector": "#first-name", "label": "First name", "type": "text", "value": "Candidate", "options": []}],
            "actions": [{"selector": "safe-action:next:0", "label": "Next"}],
        }
        bridge.save_snapshot(first)
        bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        click = bridge.pending_commands("42")[0]
        self.assertEqual(bridge.autopilot_status("42")["steps"], 0)
        bridge.add_result({"tabId": "42", "results": [{
            "commandId": click["commandId"], "selector": click["selector"], "status": "clicked",
        }]})
        self.assertEqual(bridge.autopilot_status("42")["steps"], 1)
        bridge.save_snapshot({**first, "body": "Application Loading please wait"})
        self.assertEqual(bridge.autopilot_status("42")["state"], "waiting_for_navigation")
        self.assertEqual(bridge.pending_commands("42"), [])
        bridge.save_snapshot({**first, "body": "Next application page", "documentKey": "page-b"})
        self.assertEqual(bridge.pending_commands("42")[0]["documentKey"], "page-b")

    def test_autopilot_stops_on_a_validation_error_without_a_page_change(self):
        bridge = ExtensionBridge()
        snapshot = {
            "tabId": "42", "url": "https://example.test/application", "title": "Example", "body": "Application",
            "documentKey": "page-a", "fields": [],
            "actions": [{"selector": "safe-action:next:0", "label": "Next"}],
        }
        bridge.save_snapshot(snapshot)
        bridge.start_autopilot("42", {}, {}, max_steps=2)
        click = bridge.pending_commands("42")[0]
        bridge.add_result({"tabId": "42", "results": [{
            "commandId": click["commandId"], "selector": click["selector"], "status": "clicked",
        }]})
        bridge.save_snapshot({**snapshot, "errors": ["First name is required"]})
        self.assertEqual(bridge.autopilot_status("42")["state"], "needs_user_input")

    def test_hscni_candidate_registration_shell_is_not_an_access_barrier(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://jobs-app.hscni.net/sap/bc/webdynpro/sap/hrrcf_a_startpage_ext_cand",
            "title": "Candidate Registration", "body": "Candidate Registration Save and Next National Insurance No Permanent Residence",
            "documentKey": "hscni-personal", "fields": [
                {"selector": "#first", "label": "First name", "type": "text", "value": "Candidate", "options": []},
            ],
            "actions": [{"selector": "safe-action:save and next:0", "label": "Save and Next"}],
        })
        bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        self.assertEqual(bridge.autopilot_status("42")["state"], "waiting_for_navigation")

    def test_autopilot_ignores_incidental_login_text_and_queues_safe_fields(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://jobs-app.hscni.net/application", "title": "Candidate Registration",
            "body": "Candidate Registration Help Login Password First Name",
            "fields": [
                {"selector": "#first", "label": "First name", "type": "text", "required": True, "value": "", "options": []},
            ],
        })
        bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        self.assertEqual(bridge.autopilot_status("42")["state"], "running")
        self.assertEqual(bridge.pending_commands("42")[0]["label"], "First name")

    def test_successfactors_profile_form_fills_safe_fields_despite_login_error_text(self):
        """A mixed registration form is not reduced to a login-only screen."""
        bridge = ExtensionBridge()
        snapshot = {
            "tabId": "42",
            "url": "https://career5.successfactors.eu/career",
            "title": "Career Opportunities: Data Management",
            "body": "Career Opportunities Sign In Login Password Candidate Profile",
            "errors": ["Invalid email address or password. Please re-enter your login info."],
            "fields": [
                {"selector": "#email", "label": "Valid Email *", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#email-again", "label": "Retype Valid Email *", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#first", "label": "Legal First Name *", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#last", "label": "Legal Last Name *", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#phone", "label": "Phone Number:*", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#residence", "label": "Country of Residence", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#right-to-work", "label": "Are you legally entitled to work in the country you are applying for?", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#ethnicity", "label": "Individual Race /Ethnicity", "type": "text", "required": True, "value": "", "options": []},
            ],
        }
        bridge.save_snapshot(snapshot)
        bridge.start_autopilot("42", {
            "email": "candidate@example.test", "first_name": "Candidate", "last_name": "Example",
            "phone": "07000000000", "country": "United Kingdom",
        }, {}, max_steps=2)
        queued_labels = {command["label"] for command in bridge.pending_commands("42")}
        self.assertEqual(bridge.autopilot_status("42")["state"], "running")
        self.assertEqual(queued_labels, {
            "Valid Email *", "Retype Valid Email *", "Legal First Name *", "Legal Last Name *",
            "Phone Number:*", "Country of Residence",
        })
        self.assertNotIn("Are you legally entitled to work in the country you are applying for?", queued_labels)
        self.assertNotIn("Individual Race /Ethnicity", queued_labels)

    def test_completed_sensitive_declarations_do_not_block_safe_progression(self):
        """Leave declaration values untouched, but continue when the user completed them."""
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://smartapply.indeed.com/application", "title": "Application",
            "body": "Application profile", "fields": [
                {"selector": "#first", "label": "First name", "type": "text", "required": True, "value": "Candidate", "options": []},
                {"selector": "#legal", "label": "Are you legally entitled to work in the country you are applying for?", "type": "text", "required": True, "value": "Yes", "options": []},
                {"selector": "#ethnicity", "label": "Individual Race /Ethnicity", "type": "text", "required": True, "value": "Prefer not to say", "options": []},
            ],
            "actions": [{"selector": "safe-action:continue:0", "label": "Continue"}],
        })
        bridge.start_autopilot("42", {"first_name": "Candidate"}, {}, max_steps=2)
        commands = bridge.pending_commands("42")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["kind"], "click")
        self.assertEqual(commands[0]["label"], "Continue")

    def test_hscni_empty_work_experience_does_not_skip_to_next_step(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://jobs-app.hscni.net/sap/bc/webdynpro/sap/hrrcf_a_reg_appwizard_ext",
            "title": "Application Wizard", "body": "Previous step Save and Next Work Experience No work experience maintained",
            "activeStep": "hscni:work-experience", "fields": [],
            "actions": [{"selector": "safe-action:save and next:0", "label": "Save and Next"}],
        })
        bridge.start_autopilot("42", {"employment_history": []}, {}, max_steps=2)
        status = bridge.autopilot_status("42")
        self.assertEqual(status["state"], "needs_user_input")
        self.assertIn("Work Experience is empty", status["reason"])
        self.assertEqual(bridge.pending_commands("42"), [])

    def test_bridge_allows_only_snapshotted_safe_navigation(self):
        bridge = ExtensionBridge()
        bridge.save_snapshot({
            "tabId": "42", "url": "https://example.test/application", "title": "Example",
            "actions": [
                {"selector": "button[data-automation-id='apply-manually']", "label": "Apply Manually"},
                {"selector": "#submit", "label": "Submit application"},
            ],
        })
        self.assertTrue(is_safe_click("Apply Manually"))
        self.assertTrue(is_safe_click("Next Step"))
        self.assertTrue(is_safe_click("Save & Next"))
        self.assertFalse(is_safe_click("Submit application"))
        queued = bridge.queue_commands("42", [{
            "kind": "click", "selector": "button[data-automation-id='apply-manually']", "label": "Apply Manually",
        }])
        self.assertEqual(queued, 1)
        with self.assertRaises(ValueError):
            bridge.queue_commands("42", [{
                "kind": "click", "selector": "#other", "label": "Apply Manually",
            }])

    def test_hscni_persistent_navigation_does_not_classify_personal_data_as_sensitive(self):
        snapshot = {
            "url": "https://jobs-app.hscni.net/sap/bc/webdynpro/sap/hrrcf_a_reg_applwizard_ext",
            "body": (
                "Application Wizard Please click Save and Next regularly Residency/Immigration Details "
                "Professional Registration Previous step Save and Next Personal Data First Name Last Name"
            ),
        }
        self.assertNotIn("immigration", active_form_text(snapshot))
        self.assertIn("personal data", active_form_text(snapshot))

    def test_extension_manifest_declares_no_password_access(self):
        manifest_path = Path(__file__).parents[1] / "chrome_extension" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertNotIn("debugger", manifest["permissions"])
        self.assertIn("activeTab", manifest["permissions"])

    def test_fill_plan_uses_profile_facts_for_paired_tab(self):
        plan = extension_fill_plan({
            "tabId": "42", "url": "https://example.test/apply", "fields": [
                {"selector": "#first", "label": "First name", "type": "text", "required": True, "value": "", "options": []},
                {"selector": "#country", "label": "Country", "type": "select-one", "required": True, "value": "", "options": ["United Kingdom"]},
                {"selector": "#password", "label": "Password", "type": "password", "required": True, "value": "", "options": []},
            ],
        }, {"first_name": "Candidate", "country": "United Kingdom"}, {})
        fields = {field["field"]: field for field in plan["fields"]}
        self.assertEqual(fields["First name"]["proposed_value"], "Candidate")
        self.assertEqual(fields["Country"]["proposed_value"], "United Kingdom")
        self.assertEqual(fields["Password"]["action"], "review")
        self.assertIn("Password", plan["required_missing"])

    def test_fill_plan_marks_unknown_technical_field_as_needing_input(self):
        plan = extension_fill_plan({
            "tabId": "42", "url": "https://example.test/apply", "fields": [
                {"selector": "#WD64", "label": "WD64", "type": "text", "required": False, "value": "", "options": []},
            ],
        }, {"first_name": "Candidate"}, {})
        field = plan["fields"][0]
        self.assertEqual(field["action"], "review")
        self.assertEqual(field["source"], "Needs your input")

    def test_hscni_personal_data_adapter_maps_only_exact_known_layout(self):
        raw_fields = [
            {"selector": f"#WD{index:X}", "label": f"WD{index:X}", "type": "text", "required": False, "value": "", "options": []}
            for index in range(27)
        ]
        tab = {
            "tabId": "42",
            "url": "https://jobs-app.hscni.net/sap/bc/webdynpro/sap/hrrcf_a_reg_appwizard_ext",
            "body": (
                "Personal Data National Insurance No What is your personal data and how can we contact you? "
                "Permanent Residence Communication Data Internet References " + " ".join(HSCNI_PERSONAL_DATA_VISIBLE_SEQUENCE)
            ),
            "fields": raw_fields,
        }
        raw_fields[-1]["type"] = "textarea"
        mapped = semantic_extension_fields(tab)
        self.assertEqual(mapped[4]["label"], "First Name")
        self.assertEqual(mapped[20]["label"], "E-Mail")
        plan = extension_fill_plan(tab, {
            "first_name": "Candidate", "last_name": "Example", "email": "candidate@example.test",
            "street_address": "1 Example Road", "country": "United Kingdom", "city": "Belfast",
            "state": "Antrim", "postal_code": "BT1 1AA", "phone": "07000000000", "honorific": "Mr",
        }, {})
        planned = {field["field"]: field for field in plan["fields"]}
        self.assertNotIn("National Insurance No", planned)
        self.assertEqual(planned["First Name"]["proposed_value"], "Candidate")
        self.assertEqual(planned["Permanent Residence Street/House Number"]["proposed_value"], "1 Example Road")
        self.assertIsNone(planned["Permanent Residence Street/House Number 2"]["proposed_value"])
        self.assertIsNone(planned["Temporary Residence Street/House Number"]["proposed_value"])

    def test_hscni_exposed_snapshot_maps_address_groups_without_national_insurance(self):
        raw_fields = [
            {"selector": f"#WD{index:X}", "label": label, "type": "text", "required": False, "value": "", "options": []}
            for index, label in enumerate(HSCNI_PERSONAL_DATA_EXPOSED_RAW_LABELS)
        ]
        raw_fields[-1]["type"] = "textarea"
        tab = {
            "tabId": "42",
            "url": "https://jobs-app.hscni.net/sap/bc/webdynpro/sap/hrrcf_a_startpage_ext_cand",
            "body": (
                "Personal Data National Insurance No What is your personal data and how can we contact you? "
                "Permanent Residence Communication Data Internet References " + " ".join(HSCNI_PERSONAL_DATA_VISIBLE_SEQUENCE)
            ),
            "fields": raw_fields,
        }
        plan = extension_fill_plan(tab, {
            "street_address": "1 Example Road", "country": "United Kingdom", "city": "Belfast",
            "state": "Antrim", "postal_code": "BT1 1AA",
        }, {})
        planned = {field["field"]: field for field in plan["fields"]}
        self.assertEqual(planned["Permanent Residence Street/House Number"]["proposed_value"], "1 Example Road")
        self.assertEqual(planned["Permanent Residence Country"]["proposed_value"], "United Kingdom")
        self.assertIsNone(planned["Temporary Residence Street/House Number"]["proposed_value"])
        self.assertIsNone(planned["Temporary Residence Country"]["proposed_value"])

    def test_hscni_adapter_refuses_incomplete_or_wrong_layout(self):
        fields = [
            {"selector": f"#WD{index:X}", "label": f"WD{index:X}", "type": "text", "required": False, "value": "", "options": []}
            for index in range(26)
        ]
        tab = {
            "url": "https://jobs-app.hscni.net/sap/bc/webdynpro/sap/hrrcf_a_reg_applwizard_ext",
            "body": "Personal Data National Insurance No Permanent Residence Communication Data Internet References",
            "fields": fields,
        }
        self.assertEqual(semantic_extension_fields(tab)[0]["label"], "WD0")

    def test_hscni_email_spelling_maps_to_stored_email(self):
        plan = extension_fill_plan({
            "tabId": "42", "url": "https://example.test/apply", "fields": [
                {"selector": "#email", "label": "E-Mail", "type": "text", "required": False, "value": "", "options": []},
            ],
        }, {"email": "candidate@example.test"}, {})
        self.assertEqual(plan["fields"][0]["proposed_value"], "candidate@example.test")


if __name__ == "__main__":
    unittest.main()
