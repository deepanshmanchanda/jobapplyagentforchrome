"""Local Chrome-connected application automation with an auditable safety policy.

The agent uses the candidate's already authenticated local Chrome profile through
Chrome DevTools Protocol (CDP). It does not collect passwords, solve CAPTCHAs,
or enter one-time codes. Unknown required facts always stop the run.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from shutil import which
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import Browser, Error as PlaywrightError, Page, sync_playwright

from application_tracking import ApplicationTracker
from application_workflows import validate_employment_history, workflow_for_url
from job_assistant_rebuilt import apply_reviewed_plan, inspect_application_form, requires_manual_access


AUTOMATION_MODES = ("review", "hybrid", "auto")
FINAL_BUTTON_PATTERN = re.compile(r"^(submit|submit application|send application|complete application)$", re.I)
NEXT_BUTTON_PATTERN = re.compile(r"^(next|continue|save and continue|continue application)$", re.I)
AUTH_TEXT_PATTERNS = (
    "sign in with",
    "log in with",
    "log in",
    "one-time code",
    "one time code",
    "verification code",
    "verify your identity",
    "captcha",
    "verify you are human",
)
SUCCESS_TEXT_PATTERNS = (
    "application submitted",
    "application complete",
    "thank you for applying",
    "thanks for applying",
    "application received",
)


@dataclass(frozen=True)
class AutomationPolicy:
    """A deliberate submission policy; final clicks are opt-in."""

    mode: str = "review"
    final_submission_enabled: bool = False
    hybrid_minimum_score: int = 85
    require_sponsorship_evidence: bool = True
    daily_limit: int = 70
    max_form_steps: int = 8
    enable_site_workflows: bool = False

    def __post_init__(self) -> None:
        if self.mode not in AUTOMATION_MODES:
            raise ValueError(f"mode must be one of {AUTOMATION_MODES}")
        if not 0 <= self.hybrid_minimum_score <= 100:
            raise ValueError("hybrid_minimum_score must be between 0 and 100")
        if self.daily_limit < 1 or self.max_form_steps < 1:
            raise ValueError("daily_limit and max_form_steps must be positive")

    def can_auto_progress(self, job: dict[str, Any]) -> tuple[bool, str]:
        """Decide whether a job may be advanced without a human review stop."""
        if self.mode == "review":
            return False, "Review mode requires approval before browser actions."
        if self.require_sponsorship_evidence and job.get("require_sponsorship") is True:
            if job.get("sponsorship_status") != "evidence_found":
                return False, "Sponsorship evidence is not verified for this role."
        if self.mode == "hybrid" and int(job.get("score") or 0) < self.hybrid_minimum_score:
            return False, f"Fit score is below the Hybrid threshold ({self.hybrid_minimum_score})."
        return True, "Job is eligible for autonomous progression."

    def can_submit(self, job: dict[str, Any]) -> tuple[bool, str]:
        if not self.final_submission_enabled:
            return False, "Final submission is disabled in the automation policy."
        return self.can_auto_progress(job)


def locate_chrome() -> Path | None:
    """Find a local Chrome/Chromium executable without downloading anything."""
    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files/Chromium/Application/chrome.exe"),
    ]
    executable = which("chrome") or which("chromium")
    if executable:
        candidates.append(Path(executable))
    return next((candidate for candidate in candidates if candidate.exists()), None)


def chrome_debugging_status(endpoint: str = "http://127.0.0.1:9222") -> tuple[bool, str]:
    """Check whether Chrome's local DevTools endpoint is actually usable."""
    address = endpoint.rstrip("/") + "/json/version"
    try:
        with urlopen(address, timeout=1.5) as response:
            body = response.read().decode("utf-8", errors="replace")
        if "webSocketDebuggerUrl" not in body:
            return False, "Chrome responded, but it did not expose a DevTools WebSocket."
        return True, "Chrome DevTools is ready."
    except Exception as error:
        return False, f"Chrome DevTools is not responding at {endpoint}: {error}"


def launch_chrome_for_agent(url: str, port: int = 9222, profile_dir: str | Path = "data/chrome-agent-profile") -> subprocess.Popen:
    """Start a user-visible Chrome profile that the local agent can attach to.

    The user signs in directly in this browser. Its debugging listener is bound
    to loopback only, so no network client can attach to it.
    """
    chrome = locate_chrome()
    if not chrome:
        raise RuntimeError("Google Chrome or Chromium was not found on this computer.")
    profile = Path(profile_dir).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    endpoint = f"http://127.0.0.1:{port}"
    ready, _ = chrome_debugging_status(endpoint)
    if ready:
        raise RuntimeError("An agent-enabled Chrome session is already running. Click 'Connect to Chrome'.")
    process = subprocess.Popen(
        [
            str(chrome),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--new-window",
            url,
        ],
        cwd=str(Path.cwd()),
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready, _ = chrome_debugging_status(endpoint)
        if ready:
            return process
        if process.poll() is not None:
            raise RuntimeError(f"Chrome exited during startup with exit code {process.returncode}.")
        time.sleep(0.25)
    raise RuntimeError(
        "Chrome started but its local debugging endpoint did not become ready. "
        "Close any earlier agent Chrome window and try again."
    )


class ChromeApplicationAgent:
    """Runs supported application steps in the user's CDP-connected Chrome."""

    def __init__(self, endpoint: str = "http://127.0.0.1:9222"):
        self.endpoint = endpoint.rstrip("/")
        self.playwright = None
        self.browser: Browser | None = None
        self.context = None
        self.tabs_only = False

    def connect(self) -> dict[str, str]:
        ready, reason = chrome_debugging_status(self.endpoint)
        if not ready:
            raise RuntimeError(reason)
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(self.endpoint, timeout=10_000)
        except PlaywrightError as error:
            self.playwright.stop()
            self.playwright = None
            # A tab or extension can occasionally stall Playwright's full CDP
            # attachment even though Chrome's tab endpoint remains healthy. Keep
            # the dashboard useful: it can still list and open tabs, while it
            # clearly reports that form automation must wait for a reconnection.
            tabs = self._remote_pages()
            if tabs:
                self.tabs_only = True
                return {
                    "endpoint": self.endpoint,
                    "tabs": str(len(tabs)),
                    "mode": "tabs_only",
                    "reason": str(error),
                }
            raise RuntimeError(
                "Could not connect to Chrome. Use 'Launch Chrome for agent' first, then sign in directly in that browser."
            )
        if not self.browser.contexts:
            raise RuntimeError("Connected Chrome has no browser context.")
        self.context = self.browser.contexts[0]
        return {"endpoint": self.endpoint, "tabs": str(len(self.context.pages)), "mode": "automation"}

    def disconnect(self) -> None:
        """Detach without closing the user's Chrome windows."""
        if self.playwright:
            self.playwright.stop()
        self.playwright = self.browser = self.context = None
        self.tabs_only = False

    def _require_connection(self) -> None:
        if not self.context:
            raise RuntimeError("Connect to Chrome before running an application.")

    def open_or_select(self, url: str) -> Page:
        self._require_connection()
        # ATS URLs often contain a different per-tab session query string. Prefer
        # the exact selected tab before falling back to the job's canonical URL.
        for page in self.context.pages:
            if page.url == url:
                page.bring_to_front()
                return page
        canonical = _canonical_page_url(url)
        for page in self.context.pages:
            if _canonical_page_url(page.url) == canonical:
                page.bring_to_front()
                return page
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.bring_to_front()
        return page

    def current_pages(self) -> list[dict[str, str]]:
        if self.tabs_only:
            return self._remote_pages()
        self._require_connection()
        pages = []
        for page in self.context.pages:
            try:
                title = page.title(timeout=1_500)
            except PlaywrightError:
                title = "Tab still loading"
            pages.append({"url": page.url, "title": title})
        return pages

    def inspect_job_tab(self, url: str) -> dict[str, str]:
        """Read a user-selected, already-open job tab for the dashboard form."""
        if self.tabs_only:
            target = next((page for page in self._remote_pages() if page["url"] == url), None)
            if not target:
                raise RuntimeError("The selected Chrome tab is no longer open. Refresh the tab list and try again.")
            return job_context_from_page(target["url"], target["title"], "")
        page = self.open_or_select(url)
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
        body = page.locator("body").inner_text(timeout=10_000)
        return job_context_from_page(page.url, page.title(), body)

    def open_and_inspect_job_link(self, url: str) -> dict[str, str]:
        """Open a pasted link in the agent Chrome and return cautious defaults.

        This gives the dashboard an entry point independent of a pre-existing
        browser tab. In a temporary tab-detection-only state, the link still
        opens visibly and its title is available for review.
        """
        if self.tabs_only:
            opened = self._new_remote_page(url)
            return job_context_from_page(opened["url"], opened["title"], "")
        page = self.open_or_select(url)
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        body = page.locator("body").inner_text(timeout=10_000)
        return job_context_from_page(page.url, page.title(), body)

    def _remote_pages(self) -> list[dict[str, str]]:
        """List normal page targets using Chrome's loopback-only JSON endpoint."""
        try:
            with urlopen(self.endpoint + "/json/list", timeout=3) as response:
                targets = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as error:
            raise RuntimeError(f"Chrome tab list is unavailable at {self.endpoint}: {error}") from error
        return [
            {"url": str(target.get("url", "")), "title": str(target.get("title", "") or "Tab still loading")}
            for target in targets
            if target.get("type") == "page" and str(target.get("url", "")).startswith(("http://", "https://"))
        ]

    def _new_remote_page(self, url: str) -> dict[str, str]:
        request = Request(
            self.endpoint + "/json/new?" + quote(url, safe=":/?=&_-"),
            method="PUT",
        )
        try:
            with urlopen(request, timeout=10) as response:
                target = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as error:
            raise RuntimeError(f"Chrome could not open the pasted link: {error}") from error
        return {"url": str(target.get("url", url)), "title": str(target.get("title", "") or "Tab still loading")}

    def run(
        self,
        *,
        url: str,
        role: str,
        company: str,
        profile: dict,
        answers: dict,
        resume: dict | None,
        job: dict[str, Any],
        policy: AutomationPolicy,
        tracker: ApplicationTracker,
        job_description: str = "",
        llm_provider=None,
    ) -> dict[str, Any]:
        """Fill known fields and optionally advance/submit a supported form.

        Every exit result is recorded locally. A stopped run can be repeated after
        the user completes a login or supplies a missing fact in the answer library.
        """
        page = self.open_or_select(url)
        application_id = tracker.save_form_plan(url, role, company, [], status="in_progress")
        workflow = workflow_for_url(page.url)
        tracker.record_automation_event(application_id, "run_started", {
            "mode": policy.mode, "url": page.url, "workflow": workflow["id"],
        })

        for step in range(1, policy.max_form_steps + 1):
            if _requires_human_access(page):
                return self._stop(tracker, application_id, "waiting_for_login", "waiting_for_login", {
                    "step": step,
                    "page_url": page.url,
                    "reason": "Complete sign-in, CAPTCHA, or verification directly in Chrome, then run again.",
                })

            plan = inspect_application_form(page, profile, answers, resume)
            may_progress, reason = policy.can_auto_progress(job)
            if may_progress and policy.enable_site_workflows:
                workflow_result = _run_known_site_workflow(page, workflow, profile, resume)
                if workflow_result["actions"]:
                    tracker.record_automation_event(application_id, "site_workflow_actions", {
                        "step": step, "workflow": workflow["id"], **workflow_result,
                    })
                if workflow_result["blocked"]:
                    return self._stop(tracker, application_id, "needs_user_input", "site_workflow_needs_input", {
                        "step": step, "page_url": page.url, "reason": workflow_result["blocked"],
                    })
                if workflow_result["changed"]:
                    page.wait_for_timeout(500)
                    plan = inspect_application_form(page, profile, answers, resume)
            if may_progress and llm_provider:
                try:
                    from openai_application_provider import merge_openai_drafts

                    draft_result = llm_provider.draft_form_answers(
                        fields=plan["fields"],
                        verified_facts=_verified_facts(profile, answers, job, job_description),
                        role=role,
                        company=company,
                        job_description=job_description,
                    )
                    plan = merge_openai_drafts(plan, draft_result)
                    tracker.record_automation_event(application_id, "openai_drafts_created", {
                        "step": step,
                        "fields": [draft["field_index"] for draft in draft_result["drafts"]],
                    })
                except Exception as error:  # A model outage must not cause unsupported values to be filled.
                    tracker.record_automation_event(application_id, "openai_drafts_unavailable", {
                        "step": step, "reason": str(error),
                    })
            snapshot = _snapshot(plan)
            tracker.save_form_plan(url, role, company, snapshot, status="in_progress")
            if plan["required_missing"]:
                return self._stop(tracker, application_id, "needs_user_input", "required_fields_unresolved", {
                    "step": step,
                    "page_url": page.url,
                    "fields": plan["required_missing"],
                })

            if not may_progress:
                tracker.update_status(application_id, "ready_to_submit", reason)
                tracker.record_automation_event(application_id, "review_required", {"reason": reason, "step": step})
                return {
                    "status": "review_required",
                    "application_id": application_id,
                    "reason": reason,
                    "plan": plan,
                    "workflow": workflow,
                }

            applied, cleared, attached, missing = apply_reviewed_plan(page, plan, resume)
            tracker.record_automation_event(application_id, "fields_applied", {
                "step": step, "fields": applied, "cleared": cleared, "resume_attached": attached,
            })
            if missing:
                return self._stop(tracker, application_id, "needs_user_input", "required_fields_unresolved", {
                    "step": step,
                    "page_url": page.url,
                    "fields": missing,
                })

            final_button = _find_button(page, FINAL_BUTTON_PATTERN)
            if final_button:
                may_submit, submit_reason = policy.can_submit(job)
                if may_submit and tracker.submissions_today() >= policy.daily_limit:
                    may_submit = False
                    submit_reason = f"Daily submission cap ({policy.daily_limit}) has been reached."
                if not may_submit:
                    tracker.update_status(application_id, "ready_to_submit", submit_reason)
                    tracker.record_automation_event(application_id, "final_review_required", {"reason": submit_reason})
                    return {
                        "status": "ready_to_submit",
                        "application_id": application_id,
                        "reason": submit_reason,
                        "plan": plan,
                        "workflow": workflow,
                    }
                final_button.click(timeout=10_000)
                page.wait_for_timeout(1_500)
                if _page_confirms_submission(page):
                    tracker.mark_submitted(application_id)
                    tracker.record_automation_event(application_id, "submission_confirmed", {"page_url": page.url})
                    return {"status": "submitted", "application_id": application_id, "page_url": page.url, "workflow": workflow}
                tracker.update_status(application_id, "submission_attempted", "Final submit was clicked; confirmation was not detected.")
                tracker.record_automation_event(application_id, "submission_attempted", {"page_url": page.url})
                return {
                    "status": "submission_attempted",
                    "application_id": application_id,
                    "reason": "Final submit was clicked; verify the employer confirmation page or email.",
                    "workflow": workflow,
                }

            next_button = _find_button(page, NEXT_BUTTON_PATTERN)
            if not next_button:
                tracker.update_status(application_id, "ready_to_submit", "No recognised next or final submit control was found.")
                tracker.record_automation_event(application_id, "unsupported_control", {"page_url": page.url})
                return {
                    "status": "needs_user_input",
                    "application_id": application_id,
                    "reason": "No recognised next or final submit control was found.",
                    "plan": plan,
                    "workflow": workflow,
                }
            prior_url = page.url
            next_button.click(timeout=10_000)
            page.wait_for_timeout(900)
            tracker.record_automation_event(application_id, "advanced_step", {"step": step, "from": prior_url, "to": page.url})

        return self._stop(tracker, application_id, "needs_user_input", "step_limit_reached", {
            "reason": f"Stopped after {policy.max_form_steps} form steps to avoid an uncontrolled loop.",
            "page_url": page.url,
        })

    @staticmethod
    def _stop(tracker: ApplicationTracker, application_id: int, status: str, event: str, details: dict) -> dict[str, Any]:
        tracker.update_status(application_id, status, details.get("reason", ""))
        tracker.record_automation_event(application_id, event, details)
        return {"status": status, "application_id": application_id, **details}


def _canonical_page_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _run_known_site_workflow(page: Page, workflow: dict[str, Any], profile: dict, resume: dict | None) -> dict[str, Any]:
    """Perform small, explicitly supported ATS actions before generic form fill.

    This is intentionally a narrow adapter, not a screen-coordinate macro. Each
    write comes from the locally saved CV or an employment record the candidate
    supplied with exact dates. It never attempts declarations or credentials.
    """
    result: dict[str, Any] = {"actions": [], "changed": False, "blocked": ""}
    if workflow["id"] != "oracle_recruiting":
        return result

    if resume:
        cv_result = _ensure_oracle_cv(page, resume)
        if cv_result == "attached":
            result["actions"].append("Attached the stored CV through Oracle's document dialog.")
            result["changed"] = True
        elif cv_result.startswith("blocked:"):
            result["blocked"] = cv_result.removeprefix("blocked:")
            return result

    saved_records = profile.get("employment_history", [])
    records, problems = validate_employment_history(saved_records)
    if saved_records and problems:
        result["blocked"] = "Employment history needs correction before it can be added: " + " ".join(problems)
        return result
    if records:
        employment_result = _ensure_oracle_employment(page, records)
        if employment_result == "added":
            result["actions"].append("Added one verified employment record to Oracle.")
            result["changed"] = True
        elif employment_result.startswith("blocked:"):
            result["blocked"] = employment_result.removeprefix("blocked:")
    return result


def _body_text(page: Page) -> str:
    return page.locator("body").inner_text(timeout=5_000)


def _visible_control_with_text(scope, pattern: re.Pattern[str]):
    controls = scope.locator("button, a, [role='button'], input[type='submit'], input[type='button']")
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible() or not control.is_enabled():
                continue
            label = (control.get_attribute("value") or control.inner_text() or "").strip()
            if pattern.fullmatch(label):
                return control
        except PlaywrightError:
            continue
    return None


def _ensure_oracle_cv(page: Page, resume: dict) -> str:
    """Attach the stored CV once through Oracle's inline document dialog."""
    resume_name = str(resume.get("name", "")).strip()
    if not resume_name:
        return "blocked:The stored CV has no filename. Upload it again in Your profile."
    if resume_name in _body_text(page):
        return "already_attached"
    add_button = _visible_control_with_text(page, re.compile(r"add (?:cv\s*/\s*)?document|add cv", re.I))
    if not add_button:
        return "not_available"
    try:
        add_button.click(timeout=10_000)
        page.wait_for_timeout(400)
        upload_scope = next(
            (frame for frame in page.frames if frame.locator("input[type='file']").count()),
            None,
        )
        if upload_scope is None:
            return "blocked:Oracle opened its document dialog, but no file control was found. Complete that dialog in Chrome."
        _select_oracle_cv_type(upload_scope)
        _fill_oracle_document_description(upload_scope)
        upload = upload_scope.locator("input[type='file']").first
        upload.set_input_files({
            "name": resume_name,
            "mimeType": resume.get("mime_type", "application/octet-stream"),
            "buffer": resume["data"],
        })
        upload_button = _visible_control_with_text(upload_scope, re.compile(r"upload file|upload|save", re.I))
        if not upload_button:
            return "blocked:Oracle accepted the CV file but the upload confirmation control was not recognised. Complete the document dialog in Chrome."
        upload_button.click(timeout=10_000)
        page.wait_for_timeout(700)
        return "attached" if resume_name in _body_text(page) else "blocked:Oracle did not show the uploaded CV after the upload action. Check the document section in Chrome."
    except (KeyError, PlaywrightError) as error:
        return f"blocked:Oracle CV upload stopped safely: {error}"


def _select_oracle_cv_type(scope) -> None:
    for index in range(scope.locator("select").count()):
        control = scope.locator("select").nth(index)
        try:
            options = control.locator("option").all_text_contents()
            label = next((option for option in options if re.fullmatch(r"(?:cv|curriculum vitae)", option.strip(), re.I)), None)
            if label:
                control.select_option(label=label)
                return
        except PlaywrightError:
            continue


def _fill_oracle_document_description(scope) -> None:
    fields = scope.locator("input:not([type='file']), textarea")
    for index in range(fields.count()):
        field = fields.nth(index)
        try:
            marker = " ".join([
                field.get_attribute("name") or "", field.get_attribute("id") or "",
                field.get_attribute("aria-label") or "", field.get_attribute("placeholder") or "",
            ]).casefold()
            if "description" in marker and not field.input_value().strip():
                field.fill("Curriculum Vitae")
                return
        except PlaywrightError:
            continue


def _ensure_oracle_employment(page: Page, records: list[dict[str, str | bool]]) -> str:
    """Add one missing, fully verified employment record to Oracle at a time."""
    body = _body_text(page)
    missing = next(
        (record for record in records if str(record["employer"]) not in body or str(record["title"]) not in body),
        None,
    )
    if missing is None:
        return "already_added"
    add_role = _visible_control_with_text(page, re.compile(r"add role", re.I))
    if not add_role:
        return "not_available"
    try:
        add_role.click(timeout=10_000)
        page.wait_for_timeout(350)
        current = page.locator("#P500_CURRENT_JOB")
        if current.count() and bool(missing["current"]):
            current.check(timeout=5_000)
        _fill_if_present(page, "#P500_START_DATE_input", _oracle_date(str(missing["start_date"])))
        if not bool(missing["current"]):
            _fill_if_present(page, "#P500_END_DATE_input", _oracle_date(str(missing["end_date"])))
        _fill_if_present(page, "#P500_EMPLOYER_NAME", str(missing["employer"]))
        _fill_if_present(page, "#P500_JOB_TITLE", str(missing["title"]))
        if str(missing.get("description", "")).strip():
            _fill_if_present(page, "#P500_DESCRIPTION", str(missing["description"]))
        save = _visible_control_with_text(page, re.compile(r"save", re.I))
        if not save:
            return "blocked:Oracle's employment form is open but its Save button was not recognised. Review the entered record in Chrome."
        save.click(timeout=10_000)
        page.wait_for_timeout(650)
        return "added"
    except (PlaywrightError, RuntimeError, ValueError) as error:
        return f"blocked:Oracle employment entry stopped safely: {error}"


def _fill_if_present(page: Page, selector: str, value: str) -> None:
    control = page.locator(selector)
    if not control.count():
        raise RuntimeError(f"Expected Oracle field {selector} was not found.")
    control.fill(value, timeout=5_000)


def _oracle_date(value: str) -> str:
    return date.fromisoformat(value).strftime("%d/%m/%Y")


def job_context_from_page(url: str, title: str, body: str) -> dict[str, str]:
    """Return cautious dashboard defaults from a job-listing page.

    The user still reviews the role and employer fields before the agent runs.
    """
    normalized_body = re.sub(r"\s+", " ", body.replace("’", "'")).strip()
    company = ""
    company_match = re.search(
        r"\b(?:hello[,.]?\s+)?we're\s+([^.!]{2,80}?)(?:[.!]|\s+our\b)",
        normalized_body,
        flags=re.I,
    )
    if company_match:
        company = company_match.group(1).strip(" -,:;")
    role = re.sub(r"\s+[-|]\s+.*$", "", title).strip()
    return {
        "url": url,
        "role": role,
        "company": company,
        "description": normalized_body[:30_000],
    }


def _requires_human_access(page: Page) -> bool:
    if requires_manual_access(page.title(), page.url):
        return True
    body = page.locator("body").inner_text(timeout=5_000).casefold()
    return any(pattern in body for pattern in AUTH_TEXT_PATTERNS)


def _find_button(page: Page, pattern: re.Pattern[str]):
    controls = page.locator("button, input[type='submit'], input[type='button']")
    for index in range(controls.count()):
        control = controls.nth(index)
        if not control.is_visible() or not control.is_enabled():
            continue
        label = (control.get_attribute("value") or control.inner_text() or "").strip()
        if pattern.fullmatch(label):
            return control
    return None


def _page_confirms_submission(page: Page) -> bool:
    body = page.locator("body").inner_text(timeout=5_000).casefold()
    return any(pattern in body for pattern in SUCCESS_TEXT_PATTERNS)


def _snapshot(plan: dict) -> list[dict]:
    return [
        {
            "field": item["field"],
            "value": item["proposed_value"] or "",
            "action": item["action"],
            "source": item["source"],
        }
        for item in plan["fields"]
        if item["action"] in {"fill", "clear", "keep"}
    ]


def _verified_facts(profile: dict, answers: dict, job: dict[str, Any], job_description: str) -> dict[str, str]:
    """Expose only reusable, audit-friendly facts to the answer-drafting model."""
    facts = {}
    for key in ("skills", "improvement", "motivation", "service"):
        value = str(answers.get(key, "")).strip()
        if value:
            facts[f"answer_library.{key}"] = value
    candidate_skills = [str(skill).strip() for skill in job.get("candidate_skills", []) if str(skill).strip()]
    if candidate_skills:
        facts["candidate.skills"] = ", ".join(candidate_skills)
    for key in ("location", "city"):
        value = str(profile.get(key, "")).strip()
        if value:
            facts[f"profile.{key}"] = value
    if job_description.strip():
        facts["job.description"] = job_description.strip()
    facts["job.role"] = str(job.get("role", "")).strip()
    facts["job.company"] = str(job.get("company", "")).strip()
    return {key: value for key, value in facts.items() if value}
