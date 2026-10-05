import streamlit as st

from job_assistant_rebuilt import run_app
from pipeline_dashboard import run_dashboard
from application_dashboard import run_application_dashboard


st.set_page_config(page_title="Application Copilot", page_icon="A", layout="wide")
pipeline_tab, indeed_tab, tracker_tab = st.tabs(["Job pipeline", "Application assistant", "Application tracker"])

with pipeline_tab:
	run_dashboard(configure_page=False)

with indeed_tab:
	run_app(configure_page=False)

with tracker_tab:
	run_application_dashboard(configure_page=False)