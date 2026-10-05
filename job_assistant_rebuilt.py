import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from application_tracking import ApplicationTracker
from application_workflows import validate_employment_history, workflow_for_url
from agent_field_memory import remembered_field_answer
from extension_bridge import start_bridge_server
from job_pipeline import analyze_skill_gap, load_config


PROFILE_FIELDS = {
    "honorific": "Title / honorific",
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email",
    "phone": "Phone",
    "street_address": "Address line 1",
    "address_line_2": "Address line 2 (optional)",
    "location": "Current location",
    "country": "Country of residence",
    "city": "City / town",
    "state": "County / state",
    "postal_code": "Postal code",
    "linkedin": "LinkedIn URL",
    "website": "Portfolio / website URL",
}

ANSWER_DRAFTS = {
    "motivation": (
        "I am attracted to the role of {role} at {company} because it brings together business intelligence, performance analysis, "
        "and turning data into practical decisions. At KPMG UK, I lead technical delivery for a data request "
        "and analytics product, including SQL models, dashboards, workflows, stakeholder requirements, testing, "
        "and releases. I would welcome applying that experience and learning which measures matter most "
        "to {company}'s services and customers."
    ),
    "skills": (
        "I bring more than 14 years of experience across data analytics, business intelligence, risk, and process "
        "analytics. My practical skills include SQL, Power BI and DAX, Tableau, Python, Excel/VBA, data modelling, "
        "data quality, reporting automation, and Azure. In my current KPMG UK role I lead solution design and "
        "delivery across requirements, SQL development, dashboards, workflows, testing, UAT, and production "
        "releases. I focus on making complex information clear and building solutions stakeholders can use."
    ),
    "improvement": (
        "At KPMG UK, I worked on a highly manual data-request process supporting more than 200 energy companies. "
        "I helped deliver a central portal for automated data requests and periodic submissions, with application "
        "tracking, reviewer workflows, and interactive management dashboards. This improved process visibility, "
        "reduced manual handling, and helped minimise data-entry and processing errors. I focused on the whole "
        "workflow, from requirements and data capture through review and reporting, rather than automating only "
        "one isolated task."
    ),
    "service": "",
}


@st.cache_resource
def get_browser_controller():
    return BrowserController()


@st.cache_resource
def get_extension_bridge():
    """Start/reuse the loopback server used only by the local Chrome extension."""
    return start_bridge_server()


class BrowserController:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="application-browser")
        self.executor.submit(self._start).result()

    def _start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=False)
        self.page = self.browser.new_page()
        self.page.set_default_timeout(4000)

    def _open(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return self._page_info()

    def _page_info(self):
        return {"url": self.page.url, "title": self.page.title()}

    def _analyze(self, profile, answers, resume):
        return inspect_application_form(self.page, profile, answers, resume)

    def _apply(self, plan, resume):
        return apply_reviewed_plan(self.page, plan, resume)

    def open(self, url):
        return self.executor.submit(self._open, url).result(timeout=70)

    def inspect(self):
        return self.executor.submit(self._page_info).result(timeout=10)

    def analyze(self, profile, answers, resume):
        return self.executor.submit(self._analyze, profile, answers, resume).result(timeout=20)

    def apply(self, plan, resume):
        return self.executor.submit(self._apply, plan, resume).result(timeout=35)


def normalize_application_url(value):
    value = value.strip()
    if not value:
        return None
    if not value.startswith(("https://", "http://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return value


def requires_manual_access(title, page_url=""):
    title = (title or "").casefold().replace("\u2019", "'")
    parsed = urlparse(page_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    auth_host = (
        host == "accounts.google.com"
        or host.endswith(".microsoftonline.com")
        or host == "login.live.com"
        or host.startswith("login.")
    )
    auth_path = any(part in path for part in ["/signin", "/sign-in", "/login", "/oauth/authorize"])
    return auth_host or auth_path or any(
        phrase in title
        for phrase in ["sign in", "sign-in", "log in", "login", "couldn't sign you in", "could not sign you in", "security check", "captcha", "verify you are human"]
    )


def _label(attributes):
    text = " ".join(str(value or "") for value in attributes.values())
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.sub(r"[_-]+", " ", text).casefold()


def profile_suggestion(attributes, profile):
    label = _label(attributes)
    autocomplete = attributes.get("autocomplete", "")
    if re.search(r"\b(company|employer|organisation|organization|school|university)\b", label):
        return None
    if re.search(r"\b(work experience|employment history)\b", label):
        return None
    if any(term in label for term in ["password", "verification", "one time code", "captcha", "national insurance"]):
        return None
    # These are declarations or decisions, not ordinary profile facts.  Do
    # not turn a generic word such as "country" into an answer to a right-to-
    # work, equality, previous-employment, or country-of-application question.
    if any(term in label for term in [
        "legally entitled", "right to work", "work authorisation", "work authorization",
        "immigration", "visa", "criminal conviction", "ethnic", "race", "sexual orientation",
        "religion", "disability", "reasonable adjustment", "gender", "previously employed",
        "have you been employed", "country of application", "country region code", "country/region code",
    ]):
        return None
    if any(term in label for term in ["known as", "previous name", "second name", "middle name", "preferred address", "preferred telephone"]):
        return None
    if "temporary residence" in label:
        return None
    if re.search(r"\b(street|house)\s*(number)?\s*(2|two)\b", label):
        return profile.get("address_line_2") or None
    if re.search(r"\b(title|honorific|salutation)\b", label) and not re.search(r"\b(job|role|position)\s+title\b", label):
        return profile.get("honorific")
    if "email" in label or "e mail" in label:
        return profile.get("email")
    if re.search(r"\b(alternate|secondary|other)\b", label) and any(term in label for term in ["phone", "mobile", "telephone", "contact number"]):
        return None
    if any(term in label for term in ["phone", "mobile", "telephone", "contact number"]):
        return profile.get("phone")
    if "first name" in label or "given name" in label or autocomplete == "given-name":
        return profile.get("first_name")
    if "last name" in label or "family name" in label or "surname" in label or autocomplete == "family-name":
        return profile.get("last_name")
    if "linkedin" in label or "linked in" in label:
        return profile.get("linkedin")
    if any(term in label for term in ["portfolio", "personal website", "website"]):
        return profile.get("website")
    if "postal" in label or "zip code" in label or re.search(r"\bzip\b", label):
        return profile.get("postal_code")
    if re.search(r"\b(country|nation)\s+of\s+residence\b", label):
        # The saved ``country`` field is explicitly labelled Country of
        # residence in the profile UI.  Check this before generic country
        # matching so ATS forms such as SuccessFactors use the correct fact.
        return profile.get("country")
    if re.search(r"\b(country|nation)\b", label):
        return profile.get("country")
    if re.search(r"\b(city|town)\b", label):
        return profile.get("city")
    if re.search(r"\b(county|state|province|region)\b", label):
        return profile.get("state")
    if re.search(r"\b(address\s*(line)?\s*(2|two)|address2|line\s*(2|two))\b", label):
        return profile.get("address_line_2") or None
    if re.search(r"\b(address|street)\b", label):
        return profile.get("street_address")
    if re.search(r"\b(current location|your location)\b", label):
        return profile.get("location")
    if re.search(r"\b(full name|your name|name)\b", label):
        return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    return None


def answer_suggestion(attributes, answers):
    label = _label(attributes)
    if "what attracts you" in label and ("role" in label or "rhp" in label):
        draft = answers.get("motivation", "")
        return (
            draft.replace("{role}", answers.get("role") or "this role")
            .replace("{company}", answers.get("company") or "the organisation")
            if draft
            else ""
        )
    if "knowledge" in label and "skills" in label and "experience" in label:
        return answers.get("skills", "")
    if "better way" in label or "found a better" in label:
        return answers.get("improvement", "")
    if "amazing service" in label or ("service" in label and "person delivering" in label):
        return answers.get("service", "")
    return None


def inspect_application_form(page, profile, answers, resume):
    selector = (
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):'
        'not([type="checkbox"]):not([type="radio"]):not([type="file"]), select, textarea'
    )
    controls = page.locator(selector).evaluate_all(
        """elements => elements.map(element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return {
                visible: style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
                editable: !element.disabled && !element.readOnly && !element.closest('[inert]'),
                tag: element.tagName.toLowerCase(), type: element.type || element.tagName.toLowerCase(),
                name: element.name || '', id: element.id || '', placeholder: element.placeholder || '',
                ariaLabel: element.getAttribute('aria-label') || '', autocomplete: element.autocomplete || '',
                labels: Array.from(element.labels || []).map(label => {
                    const copy = label.cloneNode(true);
                    copy.querySelectorAll('input, select, textarea, button').forEach(child => child.remove());
                    return copy.textContent.replace(/\\s+/g, ' ').trim();
                }).join(' '),
                questionText: (() => {
                    const question = element.closest('.question, .form-group');
                    if (!question) return '';
                    const copy = question.cloneNode(true);
                    copy.querySelectorAll('input, select, textarea, button').forEach(child => child.remove());
                    return copy.textContent.replace(/\\s+/g, ' ').trim();
                })(),
                required: Boolean(element.required), value: element.value || '',
                options: element.tagName === 'SELECT' ? Array.from(element.options).map(option => option.text.trim()).filter(Boolean) : []
            };
        })"""
    )
    previous_values = {profile.get("phone", ""), profile.get("location", ""), profile.get("city", ""), profile.get("state", "")}
    fields = []
    for index, control in enumerate(controls[:120]):
        if not control["visible"]:
            continue
        label = control["labels"] or control["ariaLabel"] or control["questionText"] or control["name"] or control["id"] or control["placeholder"] or "Unlabeled field"
        attributes = {**control, "labels": label}
        current = control["value"].strip()
        full_label = _label(attributes)
        ambiguous_old_value = (
            re.search(r"\b(alternate|secondary|other)\b", full_label)
            and any(term in full_label for term in ["phone", "mobile", "telephone", "contact number"])
            and current == profile.get("phone")
        ) or (
            re.search(r"\b(address|street|county|region)\b", full_label)
            and current in previous_values and bool(current)
        )
        proposal = None
        source = "Needs your input"
        action = "review"
        if ambiguous_old_value and control["editable"]:
            proposal, source, action = "", "Clear earlier ambiguous autofill", "clear"
        elif control["editable"]:
            proposal = profile_suggestion(attributes, profile)
            if proposal is None and control["tag"] == "textarea":
                proposal = answer_suggestion(attributes, answers)
                if proposal:
                    source = "Resume-grounded answer draft; review wording"
            elif proposal:
                source = "Candidate profile / supplied resume"
            if proposal:
                action = "keep" if current == proposal else "fill"
            else:
                proposal = None
        fields.append({
            "index": index, "field": label, "control": control["type"], "required": control["required"],
            "current_value": current, "proposed_value": proposal, "source": source, "action": action,
            "options": ", ".join(control["options"]),
        })

    required_missing = [
        item["field"] for item in fields
        if item["required"] and (item["action"] == "clear" or not (item["current_value"] or item["proposed_value"]))
    ]
    resume_available = False
    if resume:
        uploads = page.locator('input[type="file"]')
        for index in range(uploads.count()):
            upload = uploads.nth(index)
            info = upload.evaluate("""element => ({name: element.name || '', id: element.id || '', accept: element.accept || '', aria: element.getAttribute('aria-label') || '', labels: Array.from(element.labels || []).map(label => { const copy=label.cloneNode(true); copy.querySelectorAll('input,select,textarea,button').forEach(child=>child.remove()); return copy.textContent.replace(/\\s+/g,' ').trim(); }).join(' ')})""")
            if upload.is_enabled() and re.search(r"\b(resume|cv|curriculum vitae)\b", _label(info)):
                resume_available = True
                break
    return {
        "page_url": page.url, "page_title": page.title(), "fields": fields,
        "required_missing": required_missing, "resume_available": resume_available,
        "resume_name": resume.get("name", "") if resume_available and resume else "",
    }


def apply_reviewed_plan(page, plan, resume):
    if page.url != plan["page_url"]:
        raise RuntimeError("The form changed after analysis; analyze it again before applying.")
    if requires_manual_access(page.title(), page.url):
        raise RuntimeError("This page now requires a manual sign-in or verification step.")
    selector = (
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):'
        'not([type="checkbox"]):not([type="radio"]):not([type="file"]), select, textarea'
    )
    fields = page.locator(selector)
    applied, cleared = [], []
    for item in plan["fields"]:
        if item["action"] not in {"fill", "clear"}:
            continue
        control = fields.nth(item["index"])
        value = item["proposed_value"] or ""
        if item["control"] == "select-one" and value:
            control.select_option(label=value, timeout=2000)
        else:
            control.fill(value, timeout=2000)
        (cleared if item["action"] == "clear" else applied).append(item["field"])

    resume_attached = False
    if resume and plan["resume_available"]:
        uploads = page.locator('input[type="file"]')
        for index in range(uploads.count()):
            upload = uploads.nth(index)
            info = upload.evaluate("""element => ({name: element.name || '', id: element.id || '', accept: element.accept || '', aria: element.getAttribute('aria-label') || '', labels: Array.from(element.labels || []).map(label => { const copy=label.cloneNode(true); copy.querySelectorAll('input,select,textarea,button').forEach(child=>child.remove()); return copy.textContent.replace(/\\s+/g,' ').trim(); }).join(' ')})""")
            if upload.is_enabled() and re.search(r"\b(resume|cv|curriculum vitae)\b", _label(info)):
                upload.set_input_files({"name": resume["name"], "mimeType": resume["mime_type"], "buffer": resume["data"]})
                resume_attached = True
                break
    missing = page.locator("input[required],select[required],textarea[required]").evaluate_all(
        """elements => elements.filter(element => {
            const style = getComputedStyle(element), rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0 &&
                !(element.type === 'checkbox' || element.type === 'radio' ? element.checked : (element.value || '').trim());
        }).map(element => ({
            label: Array.from(element.labels || []).map(label => {
                const copy = label.cloneNode(true);
                copy.querySelectorAll('input,select,textarea,button').forEach(child => child.remove());
                return copy.textContent.replace(/\\s+/g, ' ').trim();
            }).join(' '),
            name: element.name || '', id: element.id || ''
        })).map(item => item.label || item.name || item.id || 'Required field')"""
    )
    return applied, cleared, resume_attached, missing


def get_profile_from_session():
    profile = {key: st.session_state.get(f"profile_{key}", "").strip() for key in PROFILE_FIELDS}
    profile["employment_history"] = st.session_state.get("employment_history", [])
    return profile


def get_answers_from_session():
    answers = {key: st.session_state.get(f"answer_{key}", value) for key, value in ANSWER_DRAFTS.items()}
    # The paired-tab route extracts context into agent_* keys, while the
    # legacy manual form uses application_* keys.  Narrative placeholders work
    # consistently in either route.
    answers["role"] = st.session_state.get("application_role") or st.session_state.get("agent_role", "")
    answers["company"] = st.session_state.get("application_company") or st.session_state.get("agent_company", "")
    return answers


HSCNI_PERSONAL_DATA_LABELS = (
    "National Insurance No", "Known As", "Previous Name", "Title", "First Name", "Second Name", "Last Name",
    "Permanent Residence Street/House Number", "Permanent Residence Street/House Number 2", "Permanent Residence Country",
    "Permanent Residence Region", "Permanent Residence Location", "Permanent Residence Postal Code",
    "Temporary Residence Street/House Number", "Temporary Residence Street/House Number 2", "Temporary Residence Country",
    "Temporary Residence Region", "Temporary Residence Location", "Temporary Residence Postal Code",
    "Preferred Address", "E-Mail", "Home Number", "Mobile Telephone Number", "Preferred Telephone Number",
    "Type of Internet Reference", "Link", "Description",
)

HSCNI_PERSONAL_DATA_VISIBLE_SEQUENCE = (
    "National Insurance No", "Known As", "Previous Name", "Title", "First Name", "Second Name", "Last Name",
    "Street/House Number", "Street/House Number 2", "Country", "Region", "Location", "Postal Code",
    "Street/House Number", "Street/House Number 2", "Country", "Region", "Location", "Postal Code",
    "Preferred Address", "E-Mail", "Home Number", "Mobile Telephone Number", "Preferred Telephone Number",
    "Type of Internet Reference", "Link", "Description",
)

# The extension intentionally never captures a National Insurance control, so
# the live paired-tab snapshot contains this exact 26-control sequence.  SAP
# exposes the two address blocks with identical visible labels; assigning the
# permanent/temporary prefixes here prevents a saved home address being copied
# into the temporary-residence fields.
HSCNI_PERSONAL_DATA_EXPOSED_LABELS = HSCNI_PERSONAL_DATA_LABELS[1:]
HSCNI_PERSONAL_DATA_EXPOSED_RAW_LABELS = (
    "Known As", "Previous Name", "Title", "First Name", "Second Name", "Last Name",
    "Street/House Number", "Street/House Number 2", "Country", "Region", "Location", "Postal Code",
    "Street/House Number", "Street/House Number 2", "Country", "Region", "Location", "Postal Code",
    "Preferred Address", "E-Mail", "Home Number", "Mobile Telephone Number", "Preferred Telephone Number",
    "Type of Internet Reference", "Link", "Description",
)


def labels_appear_in_order(body: str, labels: tuple[str, ...]) -> bool:
    position = 0
    for label in labels:
        if label == "Street/House Number":
            match = re.search(r"street/house number(?!\s*2)", body[position:])
            found = position + match.start() if match else -1
        else:
            found = body.find(label.casefold(), position)
        if found < 0:
            return False
        position = found + len(label)
    return True


def _normalise_hscni_label(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def hscni_personal_data_labels(fields: list[dict]) -> tuple[str, ...] | None:
    """Return safe semantic labels only for the exact verified HSCNI layout.

    A protected National Insurance input is removed before the snapshot leaves
    the browser.  Older tests/extension versions may still retain it as a WD
    identifier, so both the 27-control and protected 26-control layouts are
    accepted—nothing looser is positional-mapped.
    """
    raw_labels = tuple(_normalise_hscni_label(field.get("label", "")) for field in fields)
    types = [str(field.get("type", "")).casefold() for field in fields]
    all_wd = all(re.fullmatch(r"WD[0-9A-F]+", label, re.I) for label in raw_labels)
    exposed_raw = tuple(_normalise_hscni_label(label) for label in HSCNI_PERSONAL_DATA_EXPOSED_RAW_LABELS)
    if (
        len(fields) == len(HSCNI_PERSONAL_DATA_LABELS)
        and all_wd
        and types[:-1] == ["text"] * 26
        and types[-1] == "textarea"
    ):
        return HSCNI_PERSONAL_DATA_LABELS
    if (
        len(fields) == len(HSCNI_PERSONAL_DATA_EXPOSED_LABELS)
        and (all_wd or raw_labels == exposed_raw)
        and types[:-1] == ["text"] * 25
        and types[-1] == "textarea"
    ):
        return HSCNI_PERSONAL_DATA_EXPOSED_LABELS
    return None


def semantic_extension_fields(tab: dict) -> list[dict]:
    """Add verified labels for the stable HSCNI Personal Data SAP layout.

    SAP Web Dynpro often exposes controls as WD identifiers with no DOM label.
    This adapter activates only when the exact Personal Data page signature and
    the expected protected 26-control (or legacy 27-control) layout are both
    present; otherwise raw controls are left unchanged for review.
    """
    fields = [dict(raw) for raw in tab.get("fields", [])]
    url = str(tab.get("url", "")).casefold()
    body = str(tab.get("body", "")).casefold()
    labels = hscni_personal_data_labels(fields)
    expected_page = (
        "jobs-app.hscni.net" in url
        and "/sap/bc/webdynpro/" in url
        and "personal data" in body
        and "national insurance no" in body
        and "what is your personal data and how can we contact you" in body
        and "permanent residence" in body
        and "communication data" in body
        and "internet references" in body
        and labels_appear_in_order(body, HSCNI_PERSONAL_DATA_VISIBLE_SEQUENCE)
        and labels is not None
    )
    if not expected_page:
        return fields
    for raw, label in zip(fields, labels):
        raw["label"] = label
    return fields


def extension_fill_plan(tab: dict, profile: dict, answers: dict, overrides: dict | None = None) -> dict:
    """Build a source-backed plan for a tab explicitly paired by the extension."""
    overrides = dict(overrides or {})
    fields = []
    for raw in semantic_extension_fields(tab):
        label = str(raw.get("label", ""))
        # Keep other protected controls visibly marked for review in the plan,
        # but never expose a National Insurance control through this bridge.
        if "national insurance" in label.casefold():
            continue
        control = str(raw.get("type", "")).casefold()
        current = str(raw.get("value", ""))
        attributes = {
            "labels": label, "name": label, "id": raw.get("selector", ""),
            "type": control, "autocomplete": "",
        }
        proposal = str(overrides.get(str(raw.get("selector", "")), "") or "").strip() or None
        source = "Current instruction" if proposal else "Needs your input"
        if proposal is None:
            proposal = remembered_field_answer(tab.get("url", ""), label, control, answers)
            source = "Remembered manual answer" if proposal else "Needs your input"
        if proposal is None:
            proposal = profile_suggestion(attributes, profile)
            source = "Candidate profile" if proposal else "Needs your input"
        if proposal is None and control == "textarea":
            proposal = answer_suggestion(attributes, answers)
            source = "Reviewed answer library" if proposal else "Needs your input"
        action = "fill" if proposal and proposal != current else ("keep" if proposal else "review")
        fields.append({
            "selector": str(raw.get("selector", "")), "field": label, "control": control,
            "required": bool(raw.get("required", False)), "current_value": current,
            "proposed_value": proposal or None, "source": source,
            "action": action, "options": ", ".join(raw.get("options", [])),
        })
    required_missing = [
        item["field"] for item in fields
        if item["required"] and not (item["current_value"] or item["proposed_value"])
    ]
    return {"tab_id": tab["tabId"], "page_url": tab["url"], "fields": fields, "required_missing": required_missing}


def get_application_tracker():
    config = load_config(Path(__file__).with_name("config.yaml"))
    database = Path(__file__).parent / config.get("storage", {}).get("database", "data/jobs.sqlite3")
    return ApplicationTracker(database)


def remove_resume():
    st.session_state.pop("resume", None)
    st.session_state["resume_upload"] = None
    get_application_tracker().delete_resume()


def render_employment_history_editor() -> None:
    """Collect exact, user-confirmed employment facts for compatible ATS forms."""
    with st.expander("Verified employment history for ATS forms", expanded=False):
        st.caption(
            "Add only facts you have checked. The agent requires exact YYYY-MM-DD dates; "
            "it will not turn a month-only CV date into an application answer."
        )
        records = st.session_state.get("employment_history", [])
        if records:
            st.dataframe([
                {
                    "Employer": record["employer"], "Job title": record["title"],
                    "Start": record["start_date"], "End": "Present" if record["current"] else record["end_date"],
                }
                for record in records
            ], width="stretch", hide_index=True)
            remove_index = st.selectbox(
                "Remove a saved employment record", range(len(records)),
                format_func=lambda index: f"{records[index]['employer']} — {records[index]['title']}",
                key="employment_remove_index",
            )
            if st.button("Remove selected employment record"):
                st.session_state.employment_history = [
                    record for index, record in enumerate(records) if index != remove_index
                ]
                st.rerun()
        with st.form("employment_history_entry", clear_on_submit=True):
            employer = st.text_input("Employer")
            title = st.text_input("Job title")
            start_date = st.text_input("Exact start date (YYYY-MM-DD)", placeholder="YYYY-MM-DD")
            current = st.checkbox("This is my current role", value=True)
            end_date = st.text_input("Exact end date (YYYY-MM-DD)", placeholder="Leave empty for current role", disabled=current)
            description = st.text_area(
                "Role description (optional, use only verified résumé wording)",
                height=100,
            )
            add_record = st.form_submit_button("Add verified employment record")
        if add_record:
            validated, problems = validate_employment_history([{
                "employer": employer, "title": title, "start_date": start_date,
                "end_date": end_date, "current": current, "description": description,
            }])
            if problems:
                st.error(" ".join(problems))
            else:
                st.session_state.employment_history = [*records, *validated]
                st.success("Employment record added. Click Save candidate profile below to keep it for future applications.")


def run_autonomous_agent_tab(tracker, config) -> None:
    """Render the Chrome-connected, policy-controlled application runner."""
    # Import here to avoid a circular import: the automation module reuses this
    # module's source-backed form inspector.
    from autonomous_application import AutomationPolicy, ChromeApplicationAgent, launch_chrome_for_agent

    st.subheader("Autonomous application agent")
    st.caption(
        "Runs in your own Chrome profile, not an isolated browser. Sign in there yourself; "
        "the agent never reads passwords, enters one-time codes, or bypasses verification."
    )
    st.info(
        "Normal workflow: open the employer application in Chrome, click the Application Autopilot extension icon, "
        "then choose Start safe filling this page in its side panel. This dashboard is optional for profile, audit, and diagnostics."
    )
    st.caption("The side-panel agent stays in the current application tab. It does not need Chrome debugging or a separate browser window.")

    @st.fragment(run_every="3s")
    def refresh_when_a_tab_is_paired():
        """Promote a newly paired tab without asking for a dashboard refresh."""
        try:
            count = len(get_extension_bridge().tabs())
        except RuntimeError:
            count = -1
        previous = st.session_state.get("paired_extension_tab_count")
        st.session_state["paired_extension_tab_count"] = count
        if previous is not None and previous != count:
            st.rerun(scope="app")

    refresh_when_a_tab_is_paired()
    with st.expander("Extension diagnostics (normally not needed)", expanded=False):
        st.caption(
            "The side panel normally pairs and starts the current tab itself. This legacy view is only for inspecting a "
            "stalled run or testing the local bridge. The extension never sends "
            "or fills passwords, OTPs, CAPTCHA controls, account actions, or final-submit controls."
        )
        try:
            extension_bridge = get_extension_bridge()
            extension_tabs = extension_bridge.tabs()
        except RuntimeError as error:
            extension_tabs = []
            st.error(f"The local extension bridge could not start: {error}")
        if st.button("Refresh paired extension tabs (recovery)"):
            st.rerun()
        if extension_tabs:
            labels = [f"{tab['title'] or 'Untitled tab'} — {tab['url']}" for tab in extension_tabs]
            if len(extension_tabs) == 1:
                selected = 0
                st.caption("Paired application tab: " + labels[0])
            else:
                selected = st.selectbox("Paired application tab", range(len(extension_tabs)), format_func=lambda index: labels[index])
            selected_tab = extension_tabs[selected]
            st.success("This is the only tab Autopilot will control. It will not open or switch to another tab.")
            st.caption("Start once. It fills stored profile values and follows recognised non-final steps automatically; Sync is a recovery button, not a per-page task.")
            autopilot_left, autopilot_right = st.columns(2)
            if autopilot_left.button("Start safe Autopilot until final review", type="primary", key=f"start_autopilot_{selected_tab['tabId']}"):
                try:
                    from autonomous_application import job_context_from_page

                    context = job_context_from_page(selected_tab["url"], selected_tab["title"], selected_tab["body"])
                    for prefix in ("agent", "application"):
                        st.session_state.setdefault(f"{prefix}_job_url", context["url"])
                        if context["role"] and not st.session_state.get(f"{prefix}_role"):
                            st.session_state[f"{prefix}_role"] = context["role"]
                        if context["company"] and not st.session_state.get(f"{prefix}_company"):
                            st.session_state[f"{prefix}_company"] = context["company"]
                    status = extension_bridge.start_autopilot(
                        selected_tab["tabId"], get_profile_from_session(), get_answers_from_session(), max_steps=25,
                    )
                    st.success(status["reason"])
                except (RuntimeError, ValueError) as error:
                    st.error(f"Could not start Autopilot: {error}")
            try:
                autopilot_status = extension_bridge.autopilot_status(selected_tab["tabId"])
            except RuntimeError:
                autopilot_status = {"state": "off"}
            if autopilot_status.get("state") != "off":
                autopilot_right.button(
                    "Stop Autopilot", key=f"stop_autopilot_{selected_tab['tabId']}",
                    on_click=extension_bridge.stop_autopilot, args=(selected_tab["tabId"],),
                )
                st.info(
                    f"Autopilot: {autopilot_status['state'].replace('_', ' ')} — {autopilot_status['reason']} "
                    f"({autopilot_status.get('steps', 0)}/{autopilot_status.get('maxSteps', 0)} safe page steps; "
                    f"{autopilot_status.get('pending', 0)} command(s) pending)."
                )
            @st.fragment(run_every="3s")
            def render_autopilot_status():
                try:
                    status = extension_bridge.autopilot_status(selected_tab["tabId"])
                except RuntimeError:
                    status = {"state": "off", "reason": "The local bridge is unavailable.", "pending": 0}
                if status.get("state") == "off":
                    st.caption("Autopilot is ready. Start it once; this status refreshes automatically.")
                else:
                    st.info(
                        f"Autopilot: {status['state'].replace('_', ' ')} - {status['reason']} "
                        f"({status.get('steps', 0)}/{status.get('maxSteps', 0)} completed safe page steps; "
                        f"{status.get('pending', 0)} command(s) pending)."
                    )

            render_autopilot_status()
            with st.expander("Manual recovery / diagnostics", expanded=False):
                st.caption(
                    "Autopilot is the normal route. This view is only for diagnosing a pause; "
                    "it does not offer a second, competing fill or navigation flow."
                )
                st.json({
                    "current_step": selected_tab.get("activeStep") or "not detected",
                    "safe_actions_detected": [item.get("label") for item in selected_tab.get("actions", [])],
                    "safe_fields_detected": len(selected_tab.get("fields", [])),
                    "visible_errors": selected_tab.get("errors", []),
                })
        else:
            st.info("No diagnostic snapshot is paired yet. For normal use, start from the extension side panel on the application page.")
    if not st.checkbox("Show legacy Chrome debugging fallback", value=False, key="show_legacy_chrome_fallback"):
        return
    st.warning("This fallback uses a separate agent Chrome profile. It is not part of paired-tab Autopilot and is normally unnecessary.")
    endpoint = st.text_input("Chrome debugging endpoint", value="http://127.0.0.1:9222", key="agent_endpoint")
    launch_columns = st.columns(2)
    if launch_columns[0].button("Try Chrome launch (desktop session only)"):
        try:
            process = launch_chrome_for_agent("about:blank", port=int(endpoint.rsplit(":", 1)[-1]))
            st.success(f"Chrome is ready for the agent (process {process.pid}). Sign in directly in that Chrome window, then connect below.")
        except (RuntimeError, ValueError) as error:
            st.error(str(error))
    if launch_columns[1].button("Connect to Chrome"):
        previous = st.session_state.pop("chrome_agent", None)
        if previous:
            previous.disconnect()
        try:
            agent = ChromeApplicationAgent(endpoint)
            summary = agent.connect()
            st.session_state.chrome_agent = agent
            st.success(f"Connected to your Chrome: {summary['tabs']} tab(s) found.")
            if summary.get("mode") == "tabs_only":
                st.warning(
                    "Chrome tabs are visible, but its full automation connection is busy. "
                    "You can still paste/open and select a tab below; reconnect before running form automation."
                )
        except RuntimeError as error:
            st.error(str(error))

    with st.expander("Desktop Chrome fallback", expanded=False):
        st.code(
            "powershell -ExecutionPolicy Bypass -File .\\start_agent_chrome.ps1",
            language="powershell",
        )
        st.caption("Run this from the project folder in your own VS Code PowerShell terminal. It opens a visible, separate Chrome profile and keeps credentials local to that browser.")

    agent = st.session_state.get("chrome_agent")
    if not agent:
        st.info("Launch Chrome for agent, complete any sign-in there, then connect it before running a job.")
        return

    control_columns = st.columns(2)
    if control_columns[0].button("Refresh Chrome tabs"):
        st.rerun()
    if control_columns[1].button("Disconnect from Chrome"):
        agent.disconnect()
        st.session_state.pop("chrome_agent", None)
        st.rerun()

    st.caption(
        "Paste a job link here to open it in agent Chrome. The tab list contains only this separate, "
        "agent-enabled Chrome profile; ordinary Chrome windows cannot be safely attached automatically."
    )
    pasted_link = st.text_input("Paste a job or ATS link", key="agent_link_to_open")
    if st.button("Open pasted link in agent Chrome", type="primary"):
        normalized_link = normalize_application_url(pasted_link)
        if not normalized_link:
            st.error("Enter a valid HTTPS job or ATS URL.")
        else:
            try:
                context = agent.open_and_inspect_job_link(normalized_link)
                st.session_state["agent_job_url"] = context["url"]
                st.session_state["agent_role"] = context["role"]
                if context["company"]:
                    st.session_state["agent_company"] = context["company"]
                if context["description"]:
                    st.session_state["agent_job_description"] = context["description"]
                st.success("Opened the link in agent Chrome. Review the role and employer before running the agent.")
                st.rerun()
            except (RuntimeError, PlaywrightError) as error:
                st.error(f"Could not open the pasted link: {error}")

    try:
        open_tabs = [
            tab for tab in agent.current_pages()
            if urlparse(tab["url"]).scheme in {"http", "https"}
        ]
    except RuntimeError as error:
        st.error(str(error))
        return

    if open_tabs:
        tab_labels = [f"{tab['title'] or 'Untitled tab'} — {tab['url']}" for tab in open_tabs]
        selected_tab_index = st.selectbox("Open job tab", range(len(open_tabs)), format_func=lambda index: tab_labels[index])
        if st.button("Use selected Chrome tab"):
            try:
                context = agent.inspect_job_tab(open_tabs[selected_tab_index]["url"])
                st.session_state["agent_job_url"] = context["url"]
                st.session_state["agent_role"] = context["role"]
                if context["company"]:
                    st.session_state["agent_company"] = context["company"]
                st.session_state["agent_job_description"] = context["description"]
                st.success("Loaded the selected tab. Review the suggested role and company, then run the agent.")
                st.rerun()
            except (RuntimeError, PlaywrightError) as error:
                st.error(f"Could not read the selected tab: {error}")
    else:
        st.info("No HTTP(S) tabs were found. Paste a job link above to open one in agent Chrome.")

    selected_url = st.session_state.get("agent_job_url", "")
    if selected_url:
        workflow = workflow_for_url(selected_url)
        st.caption(f"Detected workflow: {workflow['name']}. {workflow['notes']}")

    with st.expander("Connected Chrome tabs"):
        try:
            st.dataframe(open_tabs, width="stretch", hide_index=True)
        except RuntimeError as error:
            st.error(str(error))

    with st.form("autonomous_application_run"):
        url = st.text_input("Job or employer ATS link", key="agent_job_url")
        role = st.text_input("Role title", key="agent_role")
        company = st.text_input("Company", key="agent_company")
        job_description = st.text_area("Job description (used for grounded AI drafts)", key="agent_job_description", height=140)
        score = st.number_input("Verified fit score", min_value=0, max_value=100, value=85, help="Use the score from your reviewed job queue; do not inflate this value to bypass Hybrid controls.")
        sponsorship_status = st.selectbox(
            "Sponsorship evidence", ["unknown", "evidence_found", "no_sponsorship"],
            format_func=lambda value: {"unknown": "Unknown - hold", "evidence_found": "Evidence found", "no_sponsorship": "Explicitly unavailable"}[value],
        )
        mode = st.selectbox("Automation mode", ["hybrid", "review", "auto"], format_func=lambda value: value.title())
        hybrid_minimum = st.slider("Hybrid auto-apply threshold", min_value=50, max_value=100, value=85)
        final_submission_enabled = st.checkbox(
            "Enable final submission for eligible jobs", value=False,
            help="When off, the agent can fill supported fields but stops before a final submit button.",
        )
        enable_site_workflows = st.checkbox(
            "Use supported ATS workflow steps (stored CV and verified employment history)",
            value=True,
            help="For supported sites, the agent can attach the stored CV and use only employment records with exact dates that you saved in Your profile.",
        )
        use_openai_drafts = st.checkbox(
            "Use OpenAI to draft safe narrative answers", value=False,
            help="Sends the job description and your reviewed answer library to your own OpenAI API account. It never drafts passwords, declarations, visa/work-rights, equality, salary, date, or address fields.",
        )
        openai_model = st.text_input(
            "OpenAI model", value=config.get("llm", {}).get("model", "gpt-5-mini"),
            disabled=not use_openai_drafts,
        )
        run_clicked = st.form_submit_button("Run autonomous application", type="primary")
    if not run_clicked:
        return
    normalized = normalize_application_url(url)
    if not normalized:
        st.error("Enter a valid HTTPS job or ATS URL.")
        return
    if not role.strip() or not company.strip():
        st.error("Enter the role title and hiring company so the application is auditable.")
        return
    if use_openai_drafts and not openai_model.strip():
        st.error("Enter the OpenAI model name or turn off AI drafting.")
        return
    try:
        policy = AutomationPolicy(
            mode=mode,
            final_submission_enabled=final_submission_enabled,
            hybrid_minimum_score=hybrid_minimum,
            require_sponsorship_evidence=config["constraints"].get("require_sponsorship") is True,
            daily_limit=int(config["limits"]["daily_volume"]),
            enable_site_workflows=enable_site_workflows,
        )
        job = {
            "score": score,
            "sponsorship_status": sponsorship_status,
            "require_sponsorship": config["constraints"].get("require_sponsorship"),
            "candidate_skills": config["profile"].get("skills", []),
            "role": role.strip(),
            "company": company.strip(),
        }
        answers = get_answers_from_session()
        answers.update({"role": role.strip(), "company": company.strip()})
        llm_provider = None
        llm_config = config.get("llm", {})
        if use_openai_drafts:
            try:
                from openai_application_provider import OpenAIApplicationProvider

                llm_provider = OpenAIApplicationProvider(
                    model=openai_model.strip(), api_key_env=llm_config.get("api_key_env", "OPENAI_API_KEY")
                )
            except RuntimeError as error:
                st.warning(f"OpenAI drafting is unavailable; continuing with deterministic, source-backed fields only. {error}")
        with st.spinner("Inspecting and progressing supported form steps in your Chrome session..."):
            result = agent.run(
                url=normalized,
                role=role.strip(),
                company=company.strip(),
                profile=get_profile_from_session(),
                answers=answers,
                resume=st.session_state.get("resume"),
                job=job,
                policy=policy,
                tracker=tracker,
                job_description=job_description,
                llm_provider=llm_provider,
            )
        status = result["status"]
        if status == "submitted":
            st.success("Employer submission confirmation was detected and recorded locally.")
        elif status == "submission_attempted":
            st.warning(result["reason"])
        elif status in {"waiting_for_login", "needs_user_input", "review_required", "ready_to_submit"}:
            st.info(result.get("reason", status.replace("_", " ").title()))
        else:
            st.warning(str(result))
        st.json(result)
    except (RuntimeError, PlaywrightError, ValueError) as error:
        st.error(f"Autonomous run stopped safely: {error}")


def run_app(configure_page=True):
    if configure_page:
        st.set_page_config(page_title="Job Application Copilot", page_icon="J", layout="wide")
    config = load_config(Path(__file__).with_name("config.yaml"))
    tracker = get_application_tracker()
    saved_answers = tracker.answer_library()
    saved_profile = tracker.load_profile()
    for key in PROFILE_FIELDS:
        widget_key = f"profile_{key}"
        if widget_key not in st.session_state:
            st.session_state[widget_key] = saved_profile.get(key, "")
    if "employment_history" not in st.session_state:
        records, _ = validate_employment_history(saved_profile.get("employment_history", []))
        st.session_state.employment_history = records
    for key, default in ANSWER_DRAFTS.items():
        widget_key = f"answer_{key}"
        if widget_key not in st.session_state:
            st.session_state[widget_key] = saved_answers.get(key, {}).get("response", default)
    if "resume" not in st.session_state:
        st.session_state.resume = tracker.load_resume()
    st.title("Job Application Copilot")
    st.caption("Pair the application tab, start Autopilot once, and let it continue through supported non-final steps in that same tab.")
    agent_tab, apply_tab, profile_tab = st.tabs(["Autonomous agent", "Application", "Your profile"])

    with agent_tab:
        run_autonomous_agent_tab(tracker, config)

    with profile_tab:
        st.subheader("Reusable candidate profile")
        left, right = st.columns(2)
        left.text_input(PROFILE_FIELDS["honorific"], key="profile_honorific", placeholder="For example: Mr")
        right.text_input("First name", key="profile_first_name")
        left.text_input("Last name", key="profile_last_name")
        right.text_input(PROFILE_FIELDS["email"], key="profile_email")
        for key in ["phone", "street_address", "address_line_2", "location", "country", "city", "state", "postal_code", "linkedin", "website"]:
            st.text_input(PROFILE_FIELDS[key], key=f"profile_{key}")
        render_employment_history_editor()
        upload = st.file_uploader("Resume", type=["pdf", "doc", "docx"], key="resume_upload")
        if upload:
            new_resume = {"name": upload.name, "mime_type": upload.type or "application/octet-stream", "data": upload.getvalue()}
            if st.session_state.get("resume") != new_resume:
                st.session_state.resume = new_resume
                tracker.save_resume(new_resume["name"], new_resume["mime_type"], new_resume["data"])
        if st.button("Save candidate profile"):
            tracker.save_profile(get_profile_from_session())
            st.success("Candidate profile saved for future applications.")
        if st.session_state.get("resume"):
            st.caption(f"Resume ready: {st.session_state.resume['name']}")
            st.button("Remove resume", on_click=remove_resume)

    with apply_tab:
        st.caption("Manual form assistant: review a job form, prepare evidence-based answers, and fill only the plan you approve. It never submits.")
        url = st.text_input("Job or employer ATS link", key="application_url")
        if st.button("Open link in browser", type="primary"):
            normalized = normalize_application_url(url)
            if not normalized:
                st.error("Enter a valid HTTPS job or ATS URL.")
            else:
                try:
                    st.session_state.current_page = get_browser_controller().open(normalized)
                    st.session_state.browser_open = True
                    st.session_state.human_verified = False
                except (PlaywrightError, TimeoutError) as error:
                    st.error(f"Could not open the link: {error}")
        if not st.session_state.get("browser_open"):
            return

        human_verified = st.checkbox("I have completed any requested sign-in or verification in the browser.", key="human_verified")
        if st.button("Refresh current page", disabled=not human_verified):
            try:
                st.session_state.current_page = get_browser_controller().inspect()
            except (PlaywrightError, TimeoutError) as error:
                st.error(f"Could not inspect the page: {error}")
        current = st.session_state.get("current_page", {})
        if not human_verified:
            st.info("Complete any requested access step in the browser, then confirm above and refresh the page.")
            return
        st.write(f"Current page: {current.get('title', '')}")
        if requires_manual_access(current.get("title"), current.get("url", "")):
            st.warning("This page is waiting for a manual sign-in or verification step. Form analysis is paused.")
            return

        st.success("Employer page detected. Review answers and the form plan before applying anything.")
        st.checkbox("I confirm this is the application page for the job I intend to apply to.", key="employer_page_confirmed")
        page_role = current.get("title", "").strip()
        role = st.text_input("Role title", value=st.session_state.get("application_role", page_role), key="application_role")
        company = st.text_input("Company", key="application_company", placeholder="Enter the hiring employer")
        role_description = st.text_area("Job description for skills comparison", key="role_description", height=120)
        with st.expander("Review written-answer drafts", expanded=True):
            st.text_area("What attracts you to the role?", value=ANSWER_DRAFTS["motivation"], key="answer_motivation", height=120)
            st.text_area("Relevant knowledge, skills and experience", value=ANSWER_DRAFTS["skills"], key="answer_skills", height=140)
            st.text_area("A time you found a better way of doing something", value=ANSWER_DRAFTS["improvement"], key="answer_improvement", height=140)
            st.text_area("A time you received amazing service", value=ANSWER_DRAFTS["service"], key="answer_service", height=100, placeholder="Add a true personal example; it cannot be inferred from your CV.")
            if st.button("Save edited answers for future applications"):
                prompts = {
                    "motivation": "Why this role and employer?",
                    "skills": "Relevant knowledge, skills and experience",
                    "improvement": "Example of improving a process",
                    "service": "Personal example of receiving amazing service",
                }
                for answer_key, prompt in prompts.items():
                    response = st.session_state.get(f"answer_{answer_key}", "").strip()
                    if response:
                        tracker.save_answer(answer_key, prompt, response, reviewed=True)
                st.success("Non-empty responses saved to your reusable answer library.")
        profile = get_profile_from_session()
        answers = get_answers_from_session()
        if st.button("Analyze visible form", disabled=not st.session_state.get("employer_page_confirmed"), type="primary"):
            try:
                st.session_state.form_plan = get_browser_controller().analyze(profile, answers, st.session_state.get("resume"))
                st.session_state.form_plan_answers = answers
                st.session_state.form_plan_url = current.get("url")
                st.session_state.approve_form_plan = False
            except (PlaywrightError, TimeoutError, RuntimeError) as error:
                st.error(f"Form analysis failed: {error}")
        plan = st.session_state.get("form_plan")
        current_plan = bool(plan and plan["page_url"] == current.get("url") and st.session_state.get("form_plan_answers") == answers)
        if plan and not current_plan:
            st.warning("The form page or drafted answers changed. Analyze again before applying.")
        if current_plan:
            st.subheader("Detected fields and proposed actions")
            rows = [{"Field": item["field"], "Required": item["required"], "Control": item["control"], "Action": item["action"], "Source": item["source"], "Proposed value": item["proposed_value"] or "", "Options": item["options"]} for item in plan["fields"]]
            st.dataframe(rows, width="stretch", hide_index=True)
            skill_gap = analyze_skill_gap(role_description, config["profile"])
            st.write(f"Resume skills found in this description: {', '.join(skill_gap['matched_skills']) or 'none detected'}")
            st.write(f"Potential skill gaps to review: {', '.join(skill_gap['gaps']) or 'none detected'}")
            if plan["required_missing"]:
                st.warning("Required fields still needing your answer: " + ", ".join(plan["required_missing"]))
            if plan["resume_available"]:
                st.caption(f"Resume upload control detected: {plan['resume_name']}")
            st.checkbox("I reviewed and approve applying the proposed values shown above.", key="approve_form_plan")
            if st.button("Apply reviewed plan", disabled=not st.session_state.approve_form_plan):
                try:
                    applied, cleared, attached, missing = get_browser_controller().apply(plan, st.session_state.get("resume"))
                    st.success(f"Applied {len(applied)} reviewed values. Resume attached: {'yes' if attached else 'no'}.")
                    if cleared:
                        st.info("Cleared ambiguous earlier autofills: " + ", ".join(cleared))
                    if missing:
                        st.warning("Required fields still need your input: " + ", ".join(missing))
                    applied_set = set(applied)
                    cleared_set = set(cleared)
                    snapshot = [
                        {
                            "field": item["field"],
                            "value": item["proposed_value"] or "",
                            "action": item["action"],
                            "source": item["source"],
                        }
                        for item in plan["fields"]
                        if item["field"] in applied_set or item["field"] in cleared_set
                    ]
                    application_id = tracker.save_form_plan(
                        current["url"], role, company, snapshot,
                        candidate_skills=skill_gap["candidate_skills"],
                        required_skills=skill_gap["role_skills_found"],
                        skill_gaps=skill_gap["gaps"],
                    )
                    st.session_state.application_record_id = application_id
                    st.success(f"Saved application draft #{application_id} with the filled-field snapshot.")
                    st.warning("Review the form in the browser. Nothing was submitted.")
                except (PlaywrightError, TimeoutError, RuntimeError) as error:
                    st.error(f"Plan could not be applied: {error}. Analyze the form again.")

        application_record_id = st.session_state.get("application_record_id")
        if application_record_id:
            if st.button("Mark this application submitted", type="primary"):
                tracker.mark_submitted(application_record_id)
                st.session_state.application_record_id = None
                st.success("Submission recorded in your application tracker.")
