from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from application_tracking import ApplicationTracker
from job_pipeline import load_config


ANSWER_PROMPTS = {
    "motivation": "Why this role / employer",
    "skills": "Relevant skills and experience",
    "improvement": "Process improvement example",
    "service": "Personal service example",
}
APPLICATION_STATUSES = [
    "in_progress",
    "waiting_for_login",
    "needs_user_input",
    "ready_to_submit",
    "submission_attempted",
    "submitted",
    "confirmation_received",
    "interview",
    "rejected",
    "offer",
    "withdrawn",
    "automation_failed",
]


def run_application_dashboard(configure_page: bool = False) -> None:
    if configure_page:
        st.set_page_config(page_title="Application Tracker", page_icon="A", layout="wide")
    config = load_config(Path(__file__).with_name("config.yaml"))
    database = Path(__file__).parent / config.get("storage", {}).get("database", "data/jobs.sqlite3")
    tracker = ApplicationTracker(database)
    applications = tracker.list_applications()
    answers = tracker.answer_library()

    st.title("My application tracker")
    st.caption("Roles, companies, stage, skills comparison, saved field plan, and browser-agent audit trail for each form.")
    metric_cols = st.columns(4)
    metric_cols[0].metric("Tracked", len(applications))
    metric_cols[1].metric("In progress", sum(record["status"] == "in_progress" for record in applications))
    metric_cols[2].metric("Submitted", sum(record["status"] == "submitted" for record in applications))
    metric_cols[3].metric("Interviews", sum(record["status"] == "interview" for record in applications))

    history_tab, answers_tab, accounts_tab = st.tabs(["Applications", "Reusable responses", "Employer accounts"])
    with accounts_tab:
        from registration_credentials import account_password, saved_accounts
        accounts = saved_accounts(database)
        st.caption("Passwords generated for employer registration are encrypted for your Windows account. A saved password does not confirm that registration succeeded.")
        if not accounts:
            st.info("No employer account passwords saved yet.")
        else:
            selected = st.selectbox("Employer account", accounts, format_func=lambda item: f"{item['site']} — {item['email']}")
            if st.button("Access saved password"):
                try:
                    password = account_password(database, selected["site"], selected["email"], create=False)
                    st.text_input("Saved password", value=password, type="password", key=f"account-password:{selected['site']}:{selected['email']}")
                except (ValueError, RuntimeError, OSError):
                    st.error("This password could not be decrypted. Use the Windows account that created it, or reset it on the employer's site.")
    with history_tab:
        if applications:
            display_rows = [
                {
                    "Role": record["role"],
                    "Company": record["company"],
                    "Status": record["status"],
                    "Updated": record["updated_at"],
                    "Submitted": record["submitted_at"] or "",
                    "Resume skills": ", ".join(record["candidate_skills"]),
                    "Role skills": ", ".join(record["required_skills"]),
                    "Skill gaps": ", ".join(record["skill_gaps"]),
                    "Job link": record["job_url"],
                }
                for record in applications
            ]
            st.dataframe(pd.DataFrame(display_rows), width="stretch", hide_index=True)
            options = {record["id"]: f"{record['role']} | {record['company']} | {record['status']}" for record in applications}
            with st.form("application_status_form"):
                selected_id = st.selectbox("Update application stage", list(options), format_func=lambda item: options[item])
                new_status = st.selectbox("Stage", APPLICATION_STATUSES)
                notes = st.text_input("Note")
                if st.form_submit_button("Save stage"):
                    tracker.update_status(selected_id, new_status, notes)
                    st.success("Application stage saved.")
                    st.rerun()
            with st.expander("Saved field snapshots"):
                for record in applications:
                    st.markdown(f"**{record['role']} | {record['company']}**")
                    snapshot = record["form_snapshot"]
                    st.dataframe(pd.DataFrame(snapshot), width="stretch", hide_index=True)
            with st.expander("Autonomous agent audit trail"):
                for record in applications:
                    events = tracker.automation_events(record["id"])
                    if events:
                        st.markdown(f"**{record['role']} | {record['company']}**")
                        st.dataframe(pd.DataFrame(events), width="stretch", hide_index=True)
        else:
            st.info("No application records yet. Analyze and apply a reviewed form plan to create a draft record.")

    with answers_tab:
        st.caption("Edit a response here; the application assistant offers it as a draft next time. Review each use for role and employer fit.")
        selected_key = st.selectbox("Response", list(ANSWER_PROMPTS), format_func=lambda key: ANSWER_PROMPTS[key])
        saved = answers.get(selected_key, {})
        prompt = st.text_input("Question pattern", value=saved.get("prompt", ANSWER_PROMPTS[selected_key]))
        response = st.text_area("Saved response", value=saved.get("response", ""), height=220)
        if st.button("Save reusable response", type="primary"):
            tracker.save_answer(selected_key, prompt, response, reviewed=True)
            st.success("Response saved for future drafts.")
            st.rerun()
        if saved:
            st.caption(f"Last saved: {saved['updated_at']} | marked reviewed: {'yes' if saved['reviewed_by_user'] else 'no'}")
