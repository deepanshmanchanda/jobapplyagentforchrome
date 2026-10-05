from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from job_pipeline import JobStore, accuracy_report, analyze_skill_gap, apply_search_preferences, load_config


OUTCOMES = ["unknown", "rejected", "interview", "offer", "hired", "ghosted"]


def run_dashboard(configure_page: bool = True) -> None:
	if configure_page:
		st.set_page_config(page_title="Job Pipeline", page_icon="J", layout="wide")
	config = load_config(Path(__file__).with_name("config.yaml"))
	database_path = Path(__file__).parent / config.get("storage", {}).get("database", "data/jobs.sqlite3")
	store = JobStore(database_path)
	preferences = store.load_search_preferences(config)
	config = apply_search_preferences(config, preferences)

	st.title("UK Data Analyst Job Pipeline")
	st.caption("Local intake, transparent fit scoring, risk classification, batch review, and outcome tracking.")
	st.warning("Dry-run only: auto-submit eligibility is recorded for analysis; no application is submitted by this app.")

	overview_tab, import_tab, review_tab, accuracy_tab, settings_tab = st.tabs(
		["Overview", "Import and score", "Batch review", "Audit and accuracy", "Configuration"]
	)
	jobs = store.list_jobs()

	with overview_tab:
		pending = [job for job in jobs if job["status"] == "pending_review"]
		auto_eligible = [job for job in jobs if job["tier"] == "auto_submit"]
		rejected = [job for job in jobs if job["status"] == "rejected"]
		metrics = st.columns(4)
		metrics[0].metric("Jobs tracked", len(jobs))
		metrics[1].metric("Pending review", len(pending))
		metrics[2].metric("Auto-tier eligible (dry-run)", len(auto_eligible))
		metrics[3].metric("Rejected / skipped", len(rejected))
		if jobs:
			st.dataframe(
				pd.DataFrame(jobs)[["title", "company", "location", "sponsorship_status", "flow_type", "score", "tier", "status", "url"]],
				width="stretch",
				hide_index=True,
			)
		else:
			st.info("No jobs imported yet. Import a CSV of listings to start the local pipeline.")

	with import_tab:
		st.subheader("Import permitted job data")
		st.caption("Upload a CSV exported from an approved job feed/API or add one listing manually. This app does not scrape job sites.")
		upload = st.file_uploader("Job listings CSV", type=["csv"], key="job_csv")
		if upload and st.button("Import CSV", type="primary"):
			try:
				result = store.import_csv(io.StringIO(upload.getvalue().decode("utf-8-sig")))
				st.success(f"Added {result['added']}; skipped {result['duplicates']} duplicates; rejected {result['invalid']} invalid rows.")
				for error in result["errors"]:
					st.error(error)
				st.rerun()
			except UnicodeDecodeError:
				st.error("Could not read this file as UTF-8 CSV.")

		with st.expander("CSV schema"):
			st.code(
				"url,title,company,location,description,application_flow_type,"
				"screening_questions_answerable,location_match,seniority_match,"
				"visa_match,sponsorship_status,sponsorship_evidence,sponsorship_provenance",
				language="text",
			)
			st.caption("Sponsorship status is evidence_found, unknown, or no_sponsorship. Include the exact evidence and its source; unknown is not treated as no.")

		with st.form("manual_job"):
			st.subheader("Add a single job")
			url = st.text_input("Job URL")
			title = st.text_input("Job title")
			company = st.text_input("Company")
			location = st.text_input("Location")
			flow = st.selectbox("Application flow", ["unknown", "easy_apply", "custom_ats"])
			description = st.text_area("Job description")
			answerable = st.checkbox("All required screening questions have explicit configured answers")
			location_match = st.checkbox("Location exactly matches my configured constraint")
			seniority_match = st.checkbox("Seniority exactly matches my configured constraint")
			visa_match = st.checkbox("Visa / sponsorship constraint exactly matches")
			sponsorship_status = st.selectbox("Sponsorship evidence", ["unknown", "evidence_found", "no_sponsorship"], format_func=lambda value: {"unknown": "Unknown — needs verification", "evidence_found": "Evidence found", "no_sponsorship": "Explicitly unavailable"}[value])
			sponsorship_evidence = st.text_area("Exact sponsorship evidence", help="Quote or briefly describe the listing evidence. Leave blank when unknown.")
			sponsorship_provenance = st.text_input("Evidence source", help="For example: employer job description, careers FAQ, or sponsor register check.")
			add_clicked = st.form_submit_button("Add listing")
		if add_clicked:
			try:
				store.add_job(
					{
						"url": url,
						"title": title,
						"company": company,
						"location": location,
						"description": description,
						"application_flow_type": flow,
						"screening_questions_answerable": answerable,
						"location_match": location_match,
						"seniority_match": seniority_match,
						"visa_match": visa_match,
						"sponsorship_status": sponsorship_status,
						"sponsorship_evidence": sponsorship_evidence,
						"sponsorship_provenance": sponsorship_provenance,
					},
					"manual_entry",
				)
				st.success("Listing recorded (or recognized as a duplicate).")
			except ValueError as error:
				st.error(str(error))
		if st.button("Score unscored jobs"):
			count = store.score_unscored(config)
			st.success(f"Scored {count} jobs with the configured offline scorer.")
			st.rerun()

	with review_tab:
		st.subheader("Daily review queue")
		pending = store.list_jobs("pending_review")
		if not pending:
			st.info("No jobs are waiting for review.")
		else:
			for job in pending:
				with st.expander(f"{job['score']}/100 · {job['title']} · {job['company']} · {job['tier']}"):
					st.write(job["decision_reason"])
					st.write(job["score_reason"])
					st.write(f"Missing configured keywords: {', '.join(json.loads(job['missing_keywords'])) or 'none'}")
					job_payload = json.loads(job["payload"])
					skill_gap = analyze_skill_gap(job_payload.get("description", ""), config["profile"])
					st.write(f"Skills evidenced in resume and named by this role: {', '.join(skill_gap['matched_skills']) or 'none detected'}")
					st.write(f"Role skills not found in configured resume profile: {', '.join(skill_gap['gaps']) or 'none detected'}")
					st.write(f"Flow: `{job['flow_type']}` · Location: {job['location']}")
					st.write(f"Sponsorship: **{job['sponsorship_status']}**")
					if job["sponsorship_evidence"]:
						st.write(f"Evidence: {job['sponsorship_evidence']} ({job['sponsorship_provenance'] or 'source not recorded'})")
					st.link_button("Open job listing", job["url"])
			ids = [job["id"] for job in pending]
			selected = st.multiselect("Select jobs for bulk decision", ids, format_func=lambda job_id: next(job["title"] for job in pending if job["id"] == job_id))
			buttons = st.columns(2)
			if buttons[0].button("Approve selected", disabled=not selected, width="stretch"):
				count = store.decide_review(selected, "approved")
				st.success(f"Approved {count} job(s) for manual application review; none were submitted.")
				st.rerun()
			if buttons[1].button("Reject selected", disabled=not selected, width="stretch"):
				count = store.decide_review(selected, "rejected")
				st.success(f"Rejected {count} job(s).")
				st.rerun()
			all_buttons = st.columns(2)
			if all_buttons[0].button("Approve all pending", width="stretch"):
				count = store.decide_review(ids, "approved")
				st.success(f"Approved {count} job(s) for manual application review; none were submitted.")
				st.rerun()
			if all_buttons[1].button("Reject all pending", width="stretch"):
				count = store.decide_review(ids, "rejected")
				st.success(f"Rejected {count} job(s).")
				st.rerun()
		st.caption("Approval moves a listing to your manually actioned set. It does not authorize or trigger automated submission.")

	with accuracy_tab:
		st.subheader("Outcome log")
		approved = [job for job in jobs if job["status"] == "approved" or job["tier"] == "auto_submit"]
		if approved:
			labels = {job["id"]: f"{job['title']} · {job['company']} · {job['url']}" for job in approved}
			with st.form("outcome_form"):
				selected_id = st.selectbox("Job", list(labels), format_func=lambda job_id: labels[job_id])
				outcome = st.selectbox("Outcome", OUTCOMES)
				if st.form_submit_button("Save outcome"):
					store.record_outcome(selected_id, outcome)
					st.success("Outcome saved to the local audit log.")
					st.rerun()
		else:
			st.info("Outcomes become available after a job is approved or qualifies for the auto tier (dry-run).")
		report = accuracy_report(jobs)
		st.write("Response rate comparison (rejected/interview/offer/hired count as a response; ghosted is no response):")
		st.dataframe(pd.DataFrame.from_dict(report, orient="index"), width="stretch")
		st.caption("Auto-tier values are eligibility estimates only. They do not represent submitted applications.")
		with st.expander("Audit log"):
			audit = store.audit_rows()
			st.dataframe(pd.DataFrame(audit), width="stretch", hide_index=True)

	with settings_tab:
		st.subheader("Saved search preferences")
		st.caption("These choices are stored locally and affect future scoring in this dashboard. They do not make any immigration or salary claim on your behalf.")
		with st.form("search_preferences"):
			redundancy_target_date = st.text_input("Redundancy target date", value=preferences.get("redundancy_target_date", ""), placeholder="YYYY-MM-DD")
			work_locations = st.text_input("Accepted work locations", value=", ".join(preferences.get("work_locations", [])), help="Separate multiple locations with commas.")
			salary_columns = st.columns(2)
			salary_minimum = salary_columns[0].text_input("Minimum salary (optional)", value=preferences.get("salary_minimum", ""))
			salary_maximum = salary_columns[1].text_input("Maximum salary (optional)", value=preferences.get("salary_maximum", ""))
			require_sponsorship = st.selectbox("Sponsorship requirement", [True, False, None], index=[True, False, None].index(preferences.get("require_sponsorship")), format_func=lambda value: {True: "Required", False: "Not required", None: "Not decided"}[value])
			visa_constraint_note = st.text_input("Visa / work-authorisation note (optional)", value=preferences.get("visa_constraint_note", ""), help="Saved for your review; no answer is auto-filled into employer forms.")
			target_titles = st.text_area("Target job titles", value="\n".join(preferences.get("target_titles", [])), help="One title per line. These affect the transparent title-match score.")
			saved = st.form_submit_button("Save search preferences")
		if saved:
			store.save_search_preferences({
				"redundancy_target_date": redundancy_target_date.strip(),
				"work_locations": [value.strip() for value in work_locations.split(",") if value.strip()],
				"salary_minimum": salary_minimum.strip(),
				"salary_maximum": salary_maximum.strip(),
				"require_sponsorship": require_sponsorship,
				"visa_constraint_note": visa_constraint_note.strip(),
				"target_titles": [value.strip() for value in target_titles.splitlines() if value.strip()],
			})
			st.success("Search preferences saved locally. Rescore unscored listings to use updated title choices.")
			st.rerun()

		st.subheader("Effective scoring configuration")
		st.json(config)
		allowed, reason = store.can_auto_submit(config)
		st.metric("Automated submission", "Disabled" if not allowed else "Unavailable")
		st.caption(reason)
		platforms = store.platform_rows()
		if platforms:
			st.subheader("Platform health")
			st.dataframe(pd.DataFrame(platforms), width="stretch", hide_index=True)
		st.caption("Edit config.yaml and restart or refresh this app to change thresholds and constraints.")
