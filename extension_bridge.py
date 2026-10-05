"""Loopback-only bridge between the dashboard and the optional Chrome extension.

The extension sends a snapshot of the tab the candidate explicitly pairs. The
dashboard may queue bounded form actions. Active runs can authorize submission
and account registration; generated credentials are encrypted with Windows DPAPI.
Password values stay out of snapshots, and verification remains user-controlled.
"""

from __future__ import annotations

import json
import threading
import time
import re
import uuid
import hashlib
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from agent_field_memory import (
    is_manual_only_field,
    is_reusable_safe_field,
    normalise_field_label,
    parse_agent_instruction,
    reusable_field_answer_key,
)


PROTECTED_TERMS = (
    "password", "passcode", "one time", "otp", "verification", "captcha",
    "sign in", "log in", "login", "create account", "submit",
    "send application", "complete application", "national insurance",
)

# The browser extension and bridge deliberately evolve together.  A protocol
# mismatch is safer than accepting commands from an older content script whose
# snapshot/action semantics we no longer trust.
BRIDGE_PROTOCOL = 8

FINAL_ACTION_PATTERN = re.compile(r"^(submit(?: application)?|send application|complete application)$", re.I)
ACCESS_ACTION_PATTERN = re.compile(r"^(sign in|log in|login)$", re.I)
REGISTER_ACTION_PATTERN = re.compile(r"^(create (?:an? |new )?account|register|sign up|signup|create candidate account)$", re.I)


def application_url_key(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.casefold().startswith("utm_") and k.casefold() not in {"source", "ref", "referrer"}]
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def is_protected(label: str) -> bool:
    normalized = " ".join(str(label or "").casefold().split())
    return any(term in normalized for term in PROTECTED_TERMS)


SAFE_CLICK_PATTERN = re.compile(
    r"^(apply manually|apply|apply for (?:this|the) (?:job|role|position)|apply now|apply online|start (?:your )?application|save\s*(?:&|and)\s*next|save and continue|next(?: step)?|continue(?: application)?)$",
    re.I,
)

AUTOPILOT_PROGRESS_LABELS = {
    "apply manually", "save and next", "save & next", "save and continue",
    "next", "next step", "continue", "continue application",
    "apply for this job", "apply now",
    "apply", "apply online", "start application", "start your application",
    "apply for the job", "apply for this role", "apply for the role",
    "apply for this position", "apply for the position",
}


def is_safe_click(label: str) -> bool:
    return bool(SAFE_CLICK_PATTERN.fullmatch(str(label or "").strip())) and not is_protected(label)


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    """Identify a page/form structure change without using typed field values."""
    material = {
        "url": snapshot.get("url", ""), "documentKey": snapshot.get("documentKey", ""),
        "title": snapshot.get("title", ""), "body": active_form_text(snapshot),
        "activeStep": snapshot.get("activeStep", ""), "errors": snapshot.get("errors", []),
        "fields": [{key: field.get(key) for key in ("selector", "label", "type", "required", "invalid", "options")}
                   for field in snapshot.get("fields", [])],
        "actions": snapshot.get("actions", []),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def active_form_text(snapshot: dict[str, Any]) -> str:
    """Remove HSCNI's persistent wizard navigation before safety classification."""
    body = str(snapshot.get("body", "")).casefold()
    if "jobs-app.hscni.net" not in str(snapshot.get("url", "")).casefold():
        return body
    # HSCNI's timeout notice also says “Save and Next”.  The real wizard
    # boundary is preceded by “Previous step”; prefer it so persistent sidebar
    # labels such as Residency/Immigration do not look like active questions.
    marker = re.search(r"previous\s+step\s+save\s*(?:&|and)\s*next", body)
    if not marker:
        marker = re.search(r"save\s*(?:&|and)\s*next", body)
    return body[marker.end():] if marker else body


def inferred_active_step(snapshot: dict[str, Any]) -> str:
    """Return a stable current-step hint without relying on a URL change.

    SAP Web Dynpro normally changes its content in place.  The visible HSCNI
    Personal Data controls are a more trustworthy step signal than the
    persistent wizard navigation that surrounds them.
    """
    supplied = " ".join(str(snapshot.get("activeStep", "")).casefold().split())
    if supplied:
        return supplied[:500]
    body = active_form_text(snapshot)
    url = str(snapshot.get("url", "")).casefold()
    if "jobs-app.hscni.net" in url:
        hscni_steps = (
            ("national insurance no", "hscni:personal-data"),
            ("permanent residence", "hscni:personal-data"),
            ("work experience", "hscni:work-experience"),
            ("cover letter", "hscni:cover-letter"),
            ("questionnaire", "hscni:questionnaire"),
            ("residency/immigration", "hscni:residency-immigration"),
            ("professional registration", "hscni:professional-registration"),
            ("referee details", "hscni:referee-details"),
            ("complete application", "hscni:complete-application"),
        )
        for marker, step in hscni_steps:
            if marker in body:
                return step
    return ""


PROGRESS_PRIORITY = (
    "save and next", "save & next", "next step", "save and continue",
    "continue application", "apply manually", "continue", "next",
    "apply for this job", "apply now",
    "apply", "apply online", "start application", "start your application",
    "apply for the job", "apply for this role", "apply for the role",
    "apply for this position", "apply for the position",
)


def choose_progress_action(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick one unambiguous non-final progression action.

    Two HSCNI ``Save and Next`` buttons are equivalent top/bottom copies, so
    either is safe.  Generic duplicate ``Next`` or ``Continue`` controls are
    not assumed equivalent; Autopilot pauses instead of guessing.
    """
    normalized = {}
    for action in actions:
        label = " ".join(str(action.get("label", "")).casefold().split())
        if label in AUTOPILOT_PROGRESS_LABELS:
            normalized.setdefault(label, []).append(action)
    for label in PROGRESS_PRIORITY:
        matches = normalized.get(label, [])
        if not matches:
            continue
        if len(matches) == 1 or label in {"save and next", "save & next"}:
            return matches[-1]
        return None
    return None


def load_saved_candidate_context(database_path: str | Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load locally saved, user-reviewed data for extension-only Autopilot.

    The Chrome extension requests an Autopilot run without receiving candidate
    values itself.  This loopback helper reads the same local SQLite profile
    used by the dashboard, so the side panel can be the daily control surface
    without turning the browser extension into a credential/profile vault.
    """
    from application_tracking import ApplicationTracker

    database = Path(database_path) if database_path else Path(__file__).parent / "data" / "jobs.sqlite3"
    tracker = ApplicationTracker(database)
    answers = {
        key: str(record.get("response", ""))
        for key, record in tracker.answer_library().items()
        if bool(record.get("reviewed_by_user", False)) and str(record.get("response", "")).strip()
    }
    return tracker.load_profile(), answers


class ExtensionBridge:
    """Thread-safe in-memory state for one local user session."""

    def __init__(self, database_path: str | Path | None = None, *, decision_planner=None):
        self._lock = threading.Lock()
        self._database_path = Path(database_path) if database_path else Path(__file__).parent / "data" / "jobs.sqlite3"
        self._tabs: dict[str, dict[str, Any]] = {}
        self._commands: dict[str, list[dict[str, Any]]] = {}
        self._results: list[dict[str, Any]] = []
        self._autopilot: dict[str, dict[str, Any]] = {}
        self._planning_locks: dict[str, threading.Lock] = {}
        self._active_tab_id: str | None = None
        self.decision_planner = decision_planner

    def save_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        tab_id = str(payload.get("tabId", "")).strip()
        url = str(payload.get("url", "")).strip()
        if not tab_id or not url.startswith(("https://", "http://")):
            raise ValueError("A paired tab needs a tabId and an HTTP(S) URL.")
        fields = []
        for raw in payload.get("fields", []):
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("label", ""))
            input_type = str(raw.get("type", "")).casefold()
            if input_type == "password" or is_protected(label):
                continue
            fields.append({
                "selector": str(raw.get("selector", ""))[:500],
                "label": label[:500],
                "type": input_type[:80],
                "required": bool(raw.get("required", False)),
                "invalid": bool(raw.get("invalid", False)),
                "value": (str(raw.get("value", "")) or (", ".join(str(name) for name in raw.get("files", [])) if input_type == "file" else ""))[:10_000],
                "maxLength": raw.get("maxLength", 0),
                "options": [str(option)[:500] for option in raw.get("options", [])[:100]],
                "accept": str(raw.get("accept", ""))[:500],
                "files": [str(name)[:500] for name in raw.get("files", [])[:10]],
                "fileRole": str(raw.get("fileRole", ""))[:30],
                "resumeSection": str(raw.get("resumeSection", ""))[:40],
                "resumeRow": str(raw.get("resumeRow", ""))[:200],
            })
        actions = []
        for raw in payload.get("actions", []):
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("label", ""))
            selector = str(raw.get("selector", ""))
            if is_safe_click(label) and selector:
                actions.append({"selector": selector[:500], "label": label[:500]})
        snapshot = {
            "tabId": tab_id,
            "url": url,
            "title": str(payload.get("title", ""))[:500],
            "body": str(payload.get("body", ""))[:50_000],
            "fields": fields,
            "resumeSections": [
                {"kind": s["kind"], "add": {
                    "selector": str(s["add"].get("selector", ""))[:500],
                    "label": str(s["add"].get("label", ""))[:500],
                } if isinstance(s.get("add"), dict) else None}
                for s in payload.get("resumeSections", []) if isinstance(s, dict)
                and s.get("kind") in {"education", "certifications", "employment"}
            ],
            "actions": actions,
            "finalActions": [
                {"selector": str(a.get("selector", ""))[:500], "label": str(a.get("label", ""))[:500]}
                for a in payload.get("finalActions", []) if isinstance(a, dict)
                and a.get("selector") and FINAL_ACTION_PATTERN.fullmatch(str(a.get("label", "")).strip())
            ],
            "accessActions": [
                {"selector": str(a.get("selector", ""))[:500], "label": str(a.get("label", ""))[:500]}
                for a in payload.get("accessActions", []) if isinstance(a, dict)
                and a.get("selector") and ACCESS_ACTION_PATTERN.fullmatch(str(a.get("label", "")).strip())
            ],
            "submissionConfirmation": payload.get("submissionConfirmation") is True,
            "registrationActions": [
                {"selector": str(a.get("selector", ""))[:500], "label": str(a.get("label", ""))[:500], "phase": a["phase"]}
                for a in payload.get("registrationActions", []) if isinstance(a, dict)
                and a.get("selector") and a.get("phase") in {"open", "prepare", "create"}
                and REGISTER_ACTION_PATTERN.fullmatch(str(a.get("label", "")).strip())
            ],
            "registrationPending": payload.get("registrationPending") is True,
            "registrationEmail": str(payload.get("registrationEmail", ""))[:500],
            "accessPending": payload.get("accessPending") is True,
            "accessReady": payload.get("accessReady") is True,
            "documentKey": str(payload.get("documentKey", ""))[:50_000],
            "errors": [str(error)[:1_000] for error in payload.get("errors", [])[:20] if str(error).strip()],
            "updatedAt": time.time(),
        }
        snapshot["activeStep"] = inferred_active_step({**snapshot, "activeStep": payload.get("activeStep", "")})
        with self._lock:
            previous = self._tabs.get(tab_id)
            fingerprint = snapshot_fingerprint(snapshot)
            snapshot["revision"] = (
                int(previous.get("revision", 0) if previous else 0) + 1
                if not previous or previous.get("fingerprint") != fingerprint
                else int(previous.get("revision", 0))
            )
            snapshot["fingerprint"] = fingerprint
            self._tabs[tab_id] = snapshot
            if bool(payload.get("paired", False)):
                self._active_tab_id = tab_id
        self._advance_autopilot(tab_id)
        return snapshot

    def tabs(self) -> list[dict[str, Any]]:
        with self._lock:
            tabs = []
            for tab in self._tabs.values():
                record = dict(tab)
                record["active"] = tab["tabId"] == self._active_tab_id
                tabs.append(record)
            return sorted(tabs, key=lambda tab: (not tab["active"], -tab["updatedAt"]))

    def active_tab_id(self) -> str | None:
        with self._lock:
            return self._active_tab_id

    def queue_commands(self, tab_id: str, commands: list[dict[str, Any]], *, expected_run: dict | None = None) -> int:
        safe_commands = []
        with self._lock:
            snapshot = self._tabs.get(str(tab_id), {})
            available_selectors = {str(field.get("selector", "")) for field in snapshot.get("fields", [])}
            run = self._autopilot.get(str(tab_id), {})
            if expected_run is not None and run is not expected_run:
                return 0
            decision_revision = run.get('decision_revision', 0)
        model_observation = None
        if self.decision_planner and run:
            from model_application_planner import observation_key
            model_observation = observation_key(snapshot)
            error_key = hashlib.sha256((model_observation + str(decision_revision)).encode()).hexdigest()
            if run.get('model_error_key') == error_key:
                return 0
            if run.get('model_retry_key') == error_key and time.monotonic() < run.get('model_retry_after', 0):
                return 0
            candidates = commands
            if commands and all(c.get('kind') == 'click' for c in commands):
                candidates = [{'kind': 'click', **action, 'authorized': True} for action in snapshot.get('actions', [])]
            self._set_autopilot_state(tab_id, 'thinking', 'Assessing the current page and choosing the next action.')
            try:
                decision_key = hashlib.sha256(json.dumps([model_observation, candidates, decision_revision,
                    run.get('recent_action_results', [])], sort_keys=True).encode()).hexdigest()
                cached = run.get('model_decision_cache', {}).get(decision_key)
                if cached is None:
                    cached = self.decision_planner.choose(snapshot, candidates, run)
                    run['model_decision_cache'] = {decision_key: cached}
                commands, decision_reason = cached
            except Exception as error:
                from model_application_planner import decision_failure
                failure_code, explanation, retryable = decision_failure(error)
                with self._lock:
                    if self._autopilot.get(str(tab_id)) is run and run.get('decision_revision', 0) == decision_revision:
                        retries = run.setdefault('model_retries', {})
                        run['model_failure_code'] = failure_code
                        if retryable and retries.get(error_key, 0) < 1:
                            retries[error_key] = retries.get(error_key, 0) + 1
                            run.update(model_retry_key=error_key, model_retry_after=time.monotonic() + 3,
                                       state='thinking', reason=explanation + ' Retrying once automatically; no browser action was sent.')
                        else:
                            run['model_error_key'] = error_key
                            run.update(state='needs_user_input', reason=explanation + ' No new browser action was queued. ' +
                                       ('The automatic retry also failed. Choose Continue to try again.' if retryable else
                                        '' if failure_code == 'request_limit' else 'Correct the issue, then choose Continue.'))
                return 0
            with self._lock:
                if self._autopilot.get(str(tab_id)) is not run or run.get('decision_revision', 0) != decision_revision:
                    return 0
                if observation_key(self._tabs.get(str(tab_id), {})) != model_observation:
                    run.update(state='waiting_for_snapshot', reason='The page changed while the model was planning. Rechecking current controls.')
                    return 0
                run['model_reason'] = decision_reason
                run.pop('model_failure_code', None)
                run.pop('model_retry_key', None)
            if not commands:
                self._set_autopilot_state(tab_id, 'needs_user_input', decision_reason)
                return 0
            attempt_key = hashlib.sha256(json.dumps([model_observation, commands, decision_revision], sort_keys=True).encode()).hexdigest()
            if run.get('model_action_attempts', {}).get(attempt_key, 0) >= 2:
                self._set_autopilot_state(tab_id, 'needs_user_input',
                    'The chosen action did not change the form after two attempts. Check the affected control before continuing.')
                return 0
        for command in commands:
            if not isinstance(command, dict):
                continue
            kind = str(command.get("kind", ""))
            label = str(command.get("label", ""))
            selector = str(command.get("selector", ""))
            safe_kind = kind in {"fill", "select", "check"}
            safe_click = kind == "click" and is_safe_click(label)
            field = next((item for item in snapshot.get("fields", []) if item["selector"] == selector), {})
            workday = (urlsplit(snapshot.get("url", "")).hostname or "").endswith(".myworkdayjobs.com")
            resume_add = (kind == "resume-add" and workday and bool(run)
                          and command.get("authorized") is True
                          and any(s["kind"] == command.get("section") and s.get("add") == {"selector": selector, "label": label}
                                  for s in snapshot.get("resumeSections", []))
                          and command.get("rowCount") == len({f["resumeRow"] for f in snapshot.get("fields", [])
                              if f.get("resumeSection") == command.get("section") and f.get("resumeRow")})
                          and bool(re.fullmatch(r"add(?: another)?", label, re.I)))
            resume_select = (kind == "resume-select" and workday and bool(run)
                             and field.get("resumeRow") and field.get("type") in {"combobox", "select-one", "select"})
            safe_kind = safe_kind or resume_select
            if kind == "check" and not (
                (urlsplit(snapshot.get("url", "")).hostname or "").endswith(".myworkdayjobs.com")
                and re.fullmatch(r"#workExperience-[\w-]+--currentlyWorkHere", selector)
                and field.get("type") == "checkbox" and label.casefold().strip() == "i currently work here"
                and command.get("value") in {"true", "false"}
            ):
                raise ValueError("Only the recognised Workday current-employment checkbox can be set.")
            upload = kind == "upload" and field.get("type") == "file" and (
                field.get("fileRole") == "resume" or bool(re.search(r"\b(resume|cv|curriculum vitae)\b", field.get("label", ""), re.I))
            ) and not field.get("files") and not field.get("value")
            submit = (kind == "submit" and run.get("auto_submit") is True
                      and command.get("authorized") is True
                      and {"selector": selector, "label": label} in snapshot.get("finalActions", []))
            authenticate = (kind == "authenticate" and bool(run) and snapshot.get("accessReady") is True
                            and command.get("authorized") is True
                            and {"selector": selector, "label": label} in snapshot.get("accessActions", []))
            register = (kind == "register" and run.get("auto_register") is True
                        and command.get("authorized") is True
                        and {"selector": selector, "label": label, "phase": command.get("phase")} in snapshot.get("registrationActions", []))
            if not (safe_kind or safe_click or upload or submit or authenticate or register or resume_add) or not selector or (
                is_manual_only_field(label) and not (submit or authenticate or register)
            ):
                raise ValueError("The extension bridge accepts only safe fill/select actions and bounded pre-form navigation.")
            if safe_kind and selector not in available_selectors:
                raise ValueError("That field is not available in the current paired-tab snapshot.")
            if safe_kind and (field.get("type") == "file" or is_manual_only_field(field.get("label", ""))):
                raise ValueError("That field requires a dedicated application action.")
            if safe_click:
                with self._lock:
                    available_actions = self._tabs.get(str(tab_id), {}).get("actions", [])
                if {"selector": selector, "label": label} not in available_actions:
                    raise ValueError("That navigation control is not available in the current paired-tab snapshot.")
            queued = {
                "commandId": uuid.uuid4().hex,
                "kind": kind,
                "selector": selector[:500],
                "label": label[:500],
                "value": str(command.get("value", ""))[:10_000],
                "documentKey": str(snapshot.get("documentKey", ""))[:50_000],
            }
            if resume_add:
                queued.update(section=command["section"], rowCount=command["rowCount"], authorized=True)
            if resume_select:
                queued["aliases"] = [str(value)[:500] for value in command.get("aliases", [])[:10]]
            if upload:
                # Never accept arbitrary file bytes from an external caller.
                # This action can attach only the candidate's saved local CV.
                from application_tracking import ApplicationTracker
                resume = ApplicationTracker(self._database_path).load_resume()
                if not resume or not resume.get("data"):
                    raise ValueError("Save your resume once in Your profile before attaching it.")
                queued["file"] = {
                    "name": Path(resume["name"]).name, "mimeType": resume["mime_type"],
                    "dataBase64": base64.b64encode(resume["data"]).decode("ascii"),
                }
            if register:
                if urlsplit(snapshot.get("url", "")).scheme != "https":
                    raise ValueError("Account registration requires an HTTPS employer page.")
                queued["phase"] = command["phase"]
                queued["value"] = ""
                if command["phase"] == "prepare":
                    from registration_credentials import account_password
                    email = str(run["profile"].get("email", "")).strip()
                    if not email or snapshot.get("registrationEmail", "").strip().casefold() != email.casefold():
                        raise ValueError("The registration email must match your saved profile email.")
                    queued["email"] = email
                    queued["password"] = account_password(self._database_path, snapshot["url"], email)
            if submit or authenticate or register:
                queued["authorized"] = True
            safe_commands.append(queued)
        with self._lock:
            if expected_run is not None and self._autopilot.get(str(tab_id)) is not expected_run:
                return 0
            if model_observation is not None and (
                self._autopilot.get(str(tab_id)) is not run or run.get('decision_revision', 0) != decision_revision
                or observation_key(self._tabs.get(str(tab_id), {})) != model_observation
            ):
                return 0
            self._commands.setdefault(tab_id, []).extend(safe_commands)
            if model_observation is not None:
                attempts = run.setdefault('model_action_attempts', {})
                attempts[attempt_key] = attempts.get(attempt_key, 0) + 1
        return len(safe_commands)

    def pending_commands(self, tab_id: str) -> list[dict[str, Any]]:
        """Return commands until the extension positively acknowledges each one."""
        with self._lock:
            return [dict(command) for command in self._commands.get(tab_id, [])]

    # Kept as a compatibility alias for local callers/tests. Commands are no
    # longer destructive reads, so a transient extension failure cannot lose a fill.
    def take_commands(self, tab_id: str) -> list[dict[str, Any]]:
        return self.pending_commands(tab_id)

    def start_autopilot(
        self,
        tab_id: str,
        profile: dict[str, Any],
        answers: dict[str, Any],
        max_steps: int = 8,
        auto_submit: bool = False,
        auto_register: bool = False,
    ) -> dict[str, Any]:
        if max_steps < 1:
            raise ValueError("max_steps must be positive.")
        with self._lock:
            # A restarted run is a fresh candidate instruction. Never replay
            # a queued action that belonged to an earlier run on this tab.
            self._commands.pop(str(tab_id), None)
            self._autopilot[str(tab_id)] = {
                "state": "waiting_for_snapshot",
                "reason": "Waiting for the paired tab's current form snapshot.",
                "steps": 0,
                "max_steps": max_steps,
                "profile": dict(profile),
                "answers": dict(answers),
                "overrides": {},
                "auto_submit": auto_submit is True,
                "auto_register": auto_register is True,
                "draft_cache": {},
                "upload_attempts": {},
                "updatedAt": time.time(),
            }
        self._advance_autopilot(str(tab_id))
        return self.autopilot_status(str(tab_id))

    def remember_manual_field(self, tab_id: str, field: dict[str, Any]) -> None:
        """Persist a trusted browser edit as a site-scoped exact field answer.

        Content scripts emit this only for trusted user input events.  The
        bridge still checks the field against its most recent paired snapshot
        so a page cannot ask the local service to remember a hidden or
        credential/declaration field.
        """
        selector = str(field.get("selector", ""))[:500]
        label = str(field.get("label", ""))[:500]
        control = str(field.get("type", "")).casefold()[:80]
        value = str(field.get("value", "")).strip()
        if not selector or not label or not value or len(value) > 10_000:
            return
        if not is_reusable_safe_field(label, control):
            return
        with self._lock:
            snapshot = self._tabs.get(str(tab_id), {})
            matched = next((item for item in snapshot.get("fields", []) if item.get("selector") == selector), None)
        if not matched:
            return
        if normalise_field_label(matched.get("label")) != normalise_field_label(label):
            return
        if str(matched.get("type", "")).casefold() != control:
            return
        if is_manual_only_field(matched.get("label", "")):
            return
        from application_tracking import ApplicationTracker

        tracker = ApplicationTracker(self._database_path)
        tracker.save_answer(
            reusable_field_answer_key(str(snapshot.get("url", "")), label, control),
            label,
            value,
            reviewed=True,
        )

    def apply_instruction(self, tab_id: str, text: Any, remember: bool = False) -> dict[str, Any]:
        """Apply one bounded, candidate-written instruction to the current tab.

        The raw instruction is not retained in Autopilot status.  It can only
        re-check the current plan or fill an exact current ordinary text field.
        """
        intent, requested_label, value = parse_agent_instruction(text)
        key = str(tab_id)
        persist = None
        structured = None
        record_key = None
        target_selector = None
        with self._lock:
            existing_run = self._autopilot.get(key)
            if existing_run:
                page = self._tabs.get(key, {})
                if existing_run.get('state') == 'submitted' or page.get('submissionConfirmation'):
                    return {'status': {'state': 'submitted', 'reason': 'The employer has confirmed this application.'},
                            'message': 'This application is already submitted.'}
                if existing_run.get('submission_started'):
                    if page.get('errors') or existing_run.get('submission_correction_mode'):
                        existing_run['submission_correction_mode'] = True
                    else:
                        raise ValueError('The earlier submission is still awaiting confirmation. Corrections can resume if the employer reports validation errors; another submission will not be sent automatically.')
                existing_run['decision_revision'] = existing_run.get('decision_revision', 0) + 1
        if intent == 'context_answer':
            with self._lock:
                context_fields = self._tabs.get(key, {}).get('fields', [])
            exact_option = any(normalise_field_label(option) == normalise_field_label(value)
                               for field in context_fields for option in field.get('options', []))
            degree_value = bool(re.fullmatch(r"(?:technical diploma|(?:high school|college|postgraduate) diploma|diploma|associate(?:'s)?(?: degree)?|bachelor(?:'s)?(?: degree)?|master(?:'s)?(?: degree)?|ph\.?d\.?|doctorate)", str(value), re.I))
            if not exact_option and not degree_value:
                intent = 'workflow'
        if intent == 'workflow':
            from application_instructions import interpret_instruction
            from application_tracking import ApplicationTracker
            from workday_qualifications import education_selection_key
            with self._lock:
                active_run = self._autopilot.get(key)
                current_page = self._tabs.get(key)
                if not active_run or not current_page:
                    raise ValueError('Start Autopilot on the current form before giving a workflow instruction.')
                self._commands.pop(key, None)
                active_run.update(state='interpreting_instruction', reason='Interpreting your request before changing the form.')
            tracker = ApplicationTracker(self._database_path)
            resume = tracker.load_resume()
            try:
                from model_application_planner import claim_model_call
                claim_model_call(active_run)
                interpreted = interpret_instruction(str(text), current_page, resume)
            except Exception as error:
                self._set_autopilot_state(key, 'needs_user_input', 'The instruction was not applied. Clarify the requested record or action.')
                if isinstance(error, ValueError):
                    raise
                raise ValueError('The instruction interpreter is unavailable. No instruction text was entered into the form.') from None
            with self._lock:
                if self._autopilot.get(key) is not active_run or self._tabs.get(key, {}).get('documentKey') != current_page.get('documentKey'):
                    raise ValueError('The application changed while interpreting your request. Send it again for the current page.')
                active_run['overrides'] = {}
                for marker in ('waiting_for_navigation_revision', 'waiting_for_navigation_document_key',
                               'waiting_for_navigation_active_step', 'waiting_for_navigation_since'):
                    active_run.pop(marker, None)
                if interpreted['intent'] == 'education_selection':
                    records = interpreted['records']
                    active_run['selected_education'] = records
                    if remember:
                        selection_key = education_selection_key(current_page['url'], resume)
                        active_run['answers'][selection_key] = json.dumps(records)
                    active_run.update(state='running', reason='Using only the education records selected by your instruction.')
                else:
                    active_run['state'] = 'running'
                    intent, requested_label, value = 'answer', interpreted['field']['label'], interpreted['value']
                    target_selector = interpreted['field']['selector']
            if interpreted['intent'] == 'education_selection':
                if remember:
                    tracker.save_answer(selection_key, 'Selected education records', json.dumps(records), reviewed=True)
                self._advance_autopilot(key)
                summary = '; '.join(record['degree'] + ' at ' + record['school'] for record in records)
                return {'status': self.autopilot_status(key), 'message': 'Understood: use only ' + summary + '.',
                        'interpretation': {'intent': 'education_selection', 'records': records}}
        with self._lock:
            run = self._autopilot.get(key)
            snapshot = self._tabs.get(key)
            if not run:
                raise ValueError("Start Autopilot on this page before sending an instruction.")
            if not snapshot:
                raise ValueError("Waiting for the current page snapshot before using an instruction.")
            if intent == "context_answer":
                eligible = [field for field in snapshot.get("fields", [])
                            if is_reusable_safe_field(field.get("label"), field.get("type"))]
                option_matches = [field for field in eligible if any(
                    normalise_field_label(option) == normalise_field_label(value)
                    for option in field.get("options", []))]
                degree_reply = bool(re.fullmatch(r"(?:technical diploma|(?:high school|college|postgraduate) diploma|diploma|associate(?:'s)?(?: degree)?|bachelor(?:'s)?(?: degree)?|master(?:'s)?(?: degree)?|ph\.?d\.?|doctorate)", str(value), re.I))
                semantic_matches = [field for field in eligible if degree_reply and normalise_field_label(field["label"]) == "degree"]
                targets = option_matches or semantic_matches
                if len(targets) > 1:
                    failed = [field for field in targets if field["selector"] in run.get("failed_field_selectors", [])]
                    if len(failed) == 1:
                        targets = failed
                if len(targets) != 1:
                    names = ", ".join(dict.fromkeys(field["label"] for field in targets))
                    raise ValueError("Which field should use that answer? " + (names + ". " if names else "")
                                     + "Use Field label: answer to identify it.")
                requested_label = targets[0]["label"]
                target_selector = targets[0]["selector"]
                intent = "answer"
            if run.get("state") == "submitted":
                return {"status": {"state": "submitted", "reason": "This application is already submitted."}, "message": "This application is already submitted."}
            if run.get("state") == "action_failed":
                run.pop("submission_started", None)
                run["upload_attempts"] = {}
            run["state"] = "running"
            if intent == "continue":
                for marker in ("waiting_for_navigation_revision", "waiting_for_navigation_document_key",
                               "waiting_for_navigation_active_step", "waiting_for_navigation_since"):
                    run.pop(marker, None)
            if intent == "answer":
                candidates = [
                    field for field in snapshot.get("fields", [])
                    if normalise_field_label(field.get("label")) == normalise_field_label(requested_label)
                    and (target_selector is None or field["selector"] == target_selector)
                ]
                if len(candidates) != 1:
                    raise ValueError("Use the exact visible label of one field on the current application page.")
                field = candidates[0]
                label = str(field.get("label", ""))
                control = str(field.get("type", "")).casefold()
                selector = str(field.get("selector", ""))
                if not selector or not is_reusable_safe_field(label, control) or is_manual_only_field(label):
                    raise ValueError("That field must be completed directly in the application page.")
                structured = {"field": label.rstrip('* ').strip(), "value": str(value)}
                if remember and field.get("resumeRow"):
                    from application_tracking import ApplicationTracker
                    from workday_qualifications import instruction_degree_key
                    record_key = instruction_degree_key(snapshot, field, ApplicationTracker(self._database_path).load_resume())
                    if record_key:
                        run["answers"][record_key] = str(value)
                # An explicit candidate instruction supersedes any queued
                # batch created from the previous snapshot.  A current-page
                # replan then queues only values valid for this same tab.
                self._commands.pop(key, None)
                override_key = f"{str(snapshot.get('documentKey', ''))}\x1f{selector}"
                run["overrides"] = {override_key: str(value)}
                run.pop("waiting_for_navigation_revision", None)
                run.pop("waiting_for_navigation_document_key", None)
                run.pop("waiting_for_navigation_active_step", None)
                run.pop("waiting_for_navigation_since", None)
                if remember and not field.get("resumeRow"):
                    persist = (str(snapshot.get("url", "")), label, control, str(value))
        if record_key:
            from application_tracking import ApplicationTracker
            ApplicationTracker(self._database_path).save_answer(record_key, structured["field"], structured["value"], reviewed=True)
        if persist:
            from application_tracking import ApplicationTracker

            page_url, label, control, answer = persist
            ApplicationTracker(self._database_path).save_answer(
                reusable_field_answer_key(page_url, label, control), label, answer, reviewed=True,
            )
        self._advance_autopilot(key)
        message = (
            "Saved a reusable safe answer for this exact question on this employer's site."
            if persist else "Instruction accepted. Rechecking the current page."
        )
        if structured:
            message = f"Understood: {structured['field']}: {structured['value']}. " + (
                "Remembered for this education record on this employer's site." if record_key else
                "Remembered for this question on this employer's site." if persist else
                "Using this answer for the current application only.")
        return {"status": self.autopilot_status(key), "message": message, "interpretation": structured}

    def stop_autopilot(self, tab_id: str) -> None:
        with self._lock:
            self._autopilot.pop(str(tab_id), None)
            # Stop is immediate, not just a status change. The browser's next
            # heartbeat must find no commands to apply on this tab.
            self._commands.pop(str(tab_id), None)

    def autopilot_status(self, tab_id: str) -> dict[str, Any]:
        with self._lock:
            state = dict(self._autopilot.get(str(tab_id), {}))
            pending = len(self._commands.get(str(tab_id), []))
        if not state:
            return {"state": "off", "reason": "Autopilot is not active.", "pending": pending}
        # Candidate facts remain local inside the bridge and are never returned to the browser/UI status endpoint.
        return {
            "state": state["state"], "reason": state["reason"] + (
                f" Model requests attempted: {state.get('model_calls', 0)}/{state.get('model_call_limit', 20)} this run."
                if self.decision_planner else ''), "steps": state["steps"],
            "maxSteps": state["max_steps"], "pending": pending, "updatedAt": state["updatedAt"],
            "modelBacked": self.decision_planner is not None,
            "modelDecision": state.get('model_reason', ''),
            "modelFailureCode": state.get('model_failure_code', ''),
            "modelCalls": state.get('model_calls', 0), "modelCallLimit": state.get('model_call_limit', 20),
        }

    def _set_autopilot_state(self, tab_id: str, state: str, reason: str, **changes: Any) -> None:
        with self._lock:
            run = self._autopilot.get(str(tab_id))
            if not run:
                return
            run.update(changes)
            run["state"] = state
            run["reason"] = reason
            run["updatedAt"] = time.time()

    def _advance_autopilot(self, tab_id: str) -> None:
        with self._lock:
            planning_lock = self._planning_locks.setdefault(str(tab_id), threading.Lock())
        if not planning_lock.acquire(blocking=False):
            return
        try:
            self._plan_autopilot(tab_id)
        finally:
            planning_lock.release()

    def _plan_autopilot(self, tab_id: str) -> None:
        """Plan one safe batch after every paired-tab snapshot.

        The planner lives in the dashboard module today. Importing it lazily
        avoids a module cycle and keeps the profile exclusively on loopback.
        """
        with self._lock:
            run = self._autopilot.get(str(tab_id))
            snapshot = self._tabs.get(str(tab_id))
            pending = bool(self._commands.get(str(tab_id)))
            if not run:
                return
            if run.get("state") in {"submitted", "action_failed", "interpreting_instruction"}:
                return
            if not snapshot:
                run.update(state="waiting_for_snapshot", reason="Waiting for the paired tab's current form snapshot.", updatedAt=time.time())
                return
            profile = dict(run["profile"])
            answers = dict(run["answers"])
            current_document_key = str(snapshot.get("documentKey", ""))
            prefix = f"{current_document_key}\x1f"
            overrides = {
                key[len(prefix):]: value
                for key, value in dict(run.get("overrides", {})).items()
                if str(key).startswith(prefix)
            }
            steps = int(run["steps"])
            max_steps = int(run["max_steps"])
            revision = int(snapshot.get("revision", 0))
            waiting_revision = run.get("waiting_for_navigation_revision")
            waiting_document_key = str(run.get("waiting_for_navigation_document_key", ""))
            waiting_active_step = str(run.get("waiting_for_navigation_active_step", ""))
            waiting_since = float(run.get("waiting_for_navigation_since", 0))
            submission_started = run.get("submission_started")
        if submission_started and (snapshot.get('submissionConfirmation') or not run.get('submission_correction_mode')):
            if snapshot.get("submissionConfirmation"):
                from application_tracking import ApplicationTracker
                tracker = ApplicationTracker(self._database_path)
                tracker.mark_submitted(run["application_id"])
                tracker.record_automation_event(run["application_id"], "submission_confirmed", {"url": snapshot["url"]})
                with self._lock:
                    self._commands.pop(str(tab_id), None)
                self._set_autopilot_state(tab_id, "submitted", "Application submitted; the employer page confirmed receipt.")
            elif snapshot.get("errors"):
                self._set_autopilot_state(tab_id, "submission_unconfirmed", "Submission was not confirmed. The page reported: " + " ".join(snapshot["errors"][:3]))
            elif time.time() - submission_started > 60:
                self._set_autopilot_state(tab_id, "submission_unconfirmed", "The submit action was sent, but no employer confirmation appeared. Check this application before retrying.")
            else:
                self._set_autopilot_state(tab_id, "waiting_for_submission", "Waiting for the employer to confirm the application was received.")
            return
        transitioned_from_navigation = False
        if waiting_revision is not None:
            # A Web Dynpro spinner/toast can mutate the DOM while the same
            # step remains visible.  Do not treat that as permission to click
            # Next again.  Require a structural document key or step change;
            # retain revision only as a legacy fallback for old snapshots.
            current_document_key = str(snapshot.get("documentKey", ""))
            current_active_step = inferred_active_step(snapshot)
            semantic_transition = (
                bool(waiting_document_key and current_document_key and current_document_key != waiting_document_key)
                or bool(waiting_active_step and current_active_step and current_active_step != waiting_active_step)
                or (
                    not waiting_document_key
                    and not waiting_active_step
                    and revision > int(waiting_revision)
                )
            )
            if snapshot.get("errors"):
                self._set_autopilot_state(
                    tab_id, "needs_user_input", "The current step reported a validation error: " + " ".join(snapshot["errors"][:3])
                )
                return
            if not semantic_transition:
                if time.time() - waiting_since < 30:
                    self._set_autopilot_state(
                        tab_id, "waiting_for_navigation", "Waiting for the current tab to finish its safe navigation."
                    )
                else:
                    self._set_autopilot_state(
                        tab_id, "paused", "The page did not change after the safe navigation step; review the visible form before continuing."
                    )
                return
            with self._lock:
                current = self._autopilot.get(str(tab_id))
                if current:
                    current.pop("waiting_for_navigation_revision", None)
                    current.pop("waiting_for_navigation_document_key", None)
                    current.pop("waiting_for_navigation_active_step", None)
                    current.pop("waiting_for_navigation_since", None)
            transitioned_from_navigation = True
        if pending:
            self._set_autopilot_state(tab_id, "running", "Waiting for the paired browser to acknowledge the queued safe action.")
            return
        # Do not classify an entire ATS page from incidental text such as
        # "login" or "password". Field-level controls are independently
        # protected by both the bridge and content script, so safe fields can
        # still be completed on a page that contains a help link or stale
        # validation message. Real validation errors after a progression click
        # continue to pause the run above.
        if (
            "jobs-app.hscni.net" in str(snapshot.get("url", "")).casefold()
            and inferred_active_step(snapshot) == "hscni:work-experience"
        ):
            # This SAP step initially exposes only Add/Edit/Delete controls,
            # not editable inputs. Do not click Save and Next over an empty
            # history: HSCNI requires each employment relationship, exact
            # dates, and any gaps to be accounted for.
            from application_workflows import validate_employment_history

            records, problems = validate_employment_history(profile.get("employment_history", []))
            if problems:
                self._set_autopilot_state(
                    tab_id, "needs_user_input",
                    "Verified employment history needs correction before HSCNI can use it: " + " ".join(problems[:2]),
                )
                return
            if not records:
                self._set_autopilot_state(
                    tab_id, "needs_user_input",
                    "HSCNI Work Experience is empty. Add verified employment records with exact dates before continuing; the agent will not invent work history or skip this section.",
                )
                return
            self._set_autopilot_state(
                tab_id, "needs_user_input",
                "HSCNI Work Experience has verified records available, but its Add/OK dialog needs a supported adapter before any details can be entered.",
            )
            return
        try:
            # Imported only when an explicit Autopilot run is active.
            from job_assistant_rebuilt import extension_fill_plan
            plan = extension_fill_plan(snapshot, profile, answers, overrides)
            from application_narratives import draft_application_narratives
            from application_tracking import ApplicationTracker
            resume = ApplicationTracker(self._database_path).load_resume()
            from workday_qualifications import qualification_plan
            from workday_qualifications import education_selection_key, qualifications as resume_qualifications
            selected_education = run.get('selected_education')
            if selected_education is None and answers.get(education_selection_key(snapshot['url'], resume)):
                saved_selection = json.loads(answers[education_selection_key(snapshot['url'], resume)])
                if isinstance(saved_selection, list) and saved_selection and all(r in resume_qualifications(resume)['education'] for r in saved_selection):
                    selected_education = saved_selection
            qualifications = qualification_plan(snapshot, resume, answers, selected_education)
            from workday_employment import workday_employment_values
            employment = workday_employment_values(snapshot, resume, profile)
            for field in plan["fields"]:
                qualification = qualifications["values"].get(field["selector"])
                if qualification and field["source"] not in {"Current instruction", "Remembered manual answer"}:
                    field.update(proposed_value=qualification["value"], source="Saved resume qualification", action="fill")
                value = employment.get(field["selector"])
                if value is not None and field["source"] not in {"Current instruction", "Remembered manual answer"}:
                    field.update(proposed_value=value, source="Saved resume employment record", action="fill")
            draft_key = str(snapshot.get("documentKey", "")) + snapshot_fingerprint(snapshot)
            drafts = run["draft_cache"].get(draft_key)
            if drafts is None:
                if self.decision_planner:
                    draft_error_key = (draft_key, run.get('decision_revision', 0))
                    if run.get('draft_error_key') == draft_error_key:
                        raise ValueError('The model could not draft answers for this page. Use Continue after checking the model connection.')
                    try:
                        drafts = self.decision_planner.draft(snapshot, profile, answers, resume, run=run)
                    except Exception as error:
                        from model_application_planner import ModelBudgetExceeded
                        if isinstance(error, ModelBudgetExceeded):
                            raise
                        run['draft_error_key'] = draft_error_key
                        raise ValueError('The model could not draft answers for this page. Use Continue after checking the model connection.') from None
                else:
                    drafts = draft_application_narratives(snapshot, profile, answers, resume=resume)
                run["draft_cache"][draft_key] = drafts
            for field in plan["fields"]:
                draft = drafts.get(field["selector"])
                if draft and not field["current_value"] and field["source"] not in {"Current instruction", "Remembered manual answer"}:
                    field.update(proposed_value=draft["value"], source=draft["source"], action="fill")
            # File attachment is an action, never a text answer. A cover letter
            # or unrelated document must not silently receive the saved CV.
            uploads = [field for field in snapshot.get("fields", []) if field.get("type") == "file"
                       and not field.get("value") and not field.get("files")
                       and (field.get("fileRole") == "resume" or (
                           field.get("fileRole") != "cover_letter"
                           and re.search(r"\b(resume|cv|curriculum vitae)\b", field.get("label", ""), re.I)))]
            if resume and uploads:
                upload = uploads[0]
                upload_key = (snapshot.get("documentKey", ""), upload["selector"])
                attempted = run["upload_attempts"].get(upload_key)
                if attempted:
                    state = "action_failed" if time.time() - attempted > 15 else "running"
                    reason = ("The resume upload did not remain attached. Check the employer's upload message."
                              if state == "action_failed" else "Waiting for the employer page to finish attaching your saved resume.")
                    self._set_autopilot_state(tab_id, state, reason)
                    return
                if not self.queue_commands(tab_id, [{"kind": "upload", "selector": upload["selector"], "label": upload["label"]}], expected_run=run):
                    return
                run["upload_attempts"][upload_key] = time.time()
                self._set_autopilot_state(tab_id, "running", "Attaching your saved resume to this application.")
                return
            plan["required_missing"] = [field["field"] for field in plan["fields"]
                                        if field["required"] and not (field["current_value"] or field["proposed_value"])]
        except Exception as error:
            self._set_autopilot_state(tab_id, "paused", f"Could not safely plan this page: {error}")
            return
        fills = [{
            "kind": "check" if field["control"] == "checkbox" and field["source"] == "Saved resume employment record" else "select" if field["control"] in {"select-one", "select"} else "fill",
            "selector": field["selector"], "label": field["field"], "value": field["proposed_value"],
            "source": field["source"],
        } for field in plan["fields"] if field["action"] == "fill" and field["selector"] and field["control"] != "file"]
        if fills:
            instructed = {field["selector"] for field in plan["fields"] if field["source"] == "Current instruction"}
            fills.sort(key=lambda command: command["selector"] not in instructed)
            for command in fills:
                qualification = qualifications["values"].get(command["selector"])
                raw = next((f for f in snapshot["fields"] if f["selector"] == command["selector"]), {})
                if raw.get("resumeRow") and raw.get("type") in {"combobox", "select-one", "select"}:
                    aliases = qualification["aliases"] if qualification and command["value"] == qualification["value"] else [command["value"]]
                    command.update(kind="resume-select", aliases=aliases)
            # Menus can stay open or cause a rerender. Observe each selection
            # before opening another dropdown or using its previous selector.
            if not self.decision_planner and any(command["kind"] == "resume-select" for command in fills):
                fills = [next(command for command in fills if command["kind"] == "resume-select")]
            # Workday removes end-date controls when current employment is
            # checked. Re-snapshot that structural change before filling dates.
            checks = [command for command in fills if command["kind"] == "check"]
            if checks and not self.decision_planner:
                fills = checks[:1]
            try:
                if not self.queue_commands(tab_id, fills, expected_run=run):
                    return
                self._set_autopilot_state(tab_id, "running", f"Filling {len(fills)} field(s) from your profile and application answers.")
            except ValueError as error:
                self._set_autopilot_state(tab_id, "paused", f"Could not queue a safe field batch: {error}")
            return
        unresolved_required = list(plan.get("required_missing", []))
        unresolved_required.extend(field['label'] for field in snapshot.get('fields', [])
            if is_manual_only_field(field.get('label')) and (field.get('required') or field.get('invalid')) and not field.get('value'))
        # SAP does not expose native required attributes. On its Personal Data
        # page, do not advance until these core facts are actually stored.
        if (
            "jobs-app.hscni.net" in str(snapshot.get("url", "")).casefold()
            and inferred_active_step(snapshot) == "hscni:personal-data"
        ):
            core = {"title", "first name", "last name", "e-mail"}
            unresolved_required.extend(
                field["field"] for field in plan["fields"]
                if field["field"].casefold() in core and not field["proposed_value"] and not field["current_value"]
            )
        if unresolved_required:
            names = ", ".join(dict.fromkeys(unresolved_required))
            self._set_autopilot_state(tab_id, "needs_user_input", f"Missing saved information for: {names}. Add it once, then continue.")
            return
        if qualifications["problems"]:
            self._set_autopilot_state(tab_id, "needs_user_input", " ".join(qualifications["problems"][:3]))
            return
        if qualifications["add"]:
            addition = qualifications["add"]
            attempt = (current_document_key, addition["section"], addition["rowCount"])
            attempts = run.setdefault("resume_add_attempts", {})
            if attempt in attempts:
                waiting = time.time() - attempts[attempt] < 20
                self._set_autopilot_state(tab_id, "running" if waiting else "paused",
                                         "Waiting for the added resume row to appear." if waiting else
                                         "The added resume row has not appeared. Check the section before continuing.")
                return
            if self.queue_commands(tab_id, [{"kind": "resume-add", **addition, "authorized": True}], expected_run=run):
                attempts[attempt] = time.time()
                self._set_autopilot_state(tab_id, "running", f"Adding the next {addition['section']} record from your resume.")
            return
        if steps >= max_steps:
            self._set_autopilot_state(tab_id, "paused", f"Reached the safe progression limit of {max_steps} steps.")
            return
        action = (next(iter(snapshot.get('actions', [])), None) if self.decision_planner
                  else choose_progress_action(snapshot.get("actions", [])))
        action_kind = "click"
        registration = snapshot.get("registrationActions", [])
        if snapshot.get("registrationPending") or (registration and not snapshot.get("accessReady")):
            if not run.get("auto_register"):
                self._set_autopilot_state(tab_id, "needs_user_input", "Enable account creation before Start, or create this employer account yourself.")
                return
            if len(registration) != 1 or snapshot.get("errors"):
                self._set_autopilot_state(tab_id, "needs_user_input", "Account registration needs your attention. Check password requirements, required terms, or verification on the page, then Continue.")
                return
            try:
                if not self.queue_commands(tab_id, [{"kind": "register", **registration[0], "authorized": True}], expected_run=run):
                    return
                if registration[0]["phase"] == "prepare":
                    self._set_autopilot_state(tab_id, "running", "Filling a unique password encrypted locally for this employer account.")
                else:
                    self._set_autopilot_state(tab_id, "waiting_for_navigation", "Continuing employer account registration in this tab.",
                                              waiting_for_navigation_revision=revision,
                                              waiting_for_navigation_document_key=current_document_key,
                                              waiting_for_navigation_active_step=inferred_active_step(snapshot),
                                              waiting_for_navigation_since=time.time())
            except (ValueError, RuntimeError, OSError) as error:
                self._set_autopilot_state(tab_id, "needs_user_input", str(error))
            return
        if snapshot.get("accessPending") or (snapshot.get("accessReady") and snapshot.get("accessActions")):
            if snapshot.get("accessReady") and len(snapshot.get("accessActions", [])) == 1:
                action = snapshot["accessActions"][0]
                action_kind = "authenticate"
            elif not snapshot.get("accessReady"):
                self._set_autopilot_state(tab_id, "needs_user_input", "This site needs a sign-in or verification step. Chrome can use your saved login; Autopilot will continue when the form is ready.")
                return
        if not action and snapshot.get("finalActions"):
            if run.get('submission_started'):
                self._set_autopilot_state(tab_id, 'submission_unconfirmed',
                    'Corrections can be made, but the earlier submission remains unconfirmed. Verify its outcome on the employer page before submitting again; no duplicate submission will be sent automatically.')
                return
            if not run.get("auto_submit"):
                self._set_autopilot_state(tab_id, "ready_to_submit", "Application prepared. Automatic submission is off for this run.")
                return
            if len(snapshot["finalActions"]) != 1:
                self._set_autopilot_state(tab_id, "needs_user_input", "More than one submission control is visible; choose the intended application.")
                return
            if snapshot.get("errors") or any(field.get("invalid") for field in snapshot.get("fields", [])):
                self._set_autopilot_state(tab_id, "needs_user_input", "The page still reports a validation error. Resolve it before submission.")
                return
            if snapshot.get("submissionConfirmation"):
                self._set_autopilot_state(tab_id, "submitted", "This page already confirms an application was received.")
                return
            tracker = ApplicationTracker(self._database_path)
            job_url = application_url_key(snapshot["url"])
            application_id = tracker.save_form_plan(job_url, snapshot.get("title", ""), "", plan["fields"])
            if not tracker.claim_submission(job_url, application_id):
                self._set_autopilot_state(tab_id, "submission_unconfirmed", "A submission was already attempted for this application. Check its status before sending it again.")
                return
            final = snapshot["finalActions"][0]
            try:
                if not self.queue_commands(tab_id, [{"kind": "submit", **final, "authorized": True}], expected_run=run):
                    tracker.release_submission(job_url)
                    return
            except ValueError:
                tracker.release_submission(job_url)
                raise
            tracker.update_status(application_id, "submission_attempted")
            tracker.record_automation_event(application_id, "submission_queued", {"url": job_url})
            self._set_autopilot_state(tab_id, "waiting_for_submission", "Submitting this application and waiting for confirmation.",
                                      submission_started=time.time(), application_id=application_id, submission_url=job_url)
            return
        if not action:
            if self.decision_planner:
                self.queue_commands(tab_id, [], expected_run=run)
                return
            self._set_autopilot_state(
                tab_id, "paused", "No unambiguous non-final Next or Continue control is available on this page."
            )
            return
        try:
            if not self.queue_commands(tab_id, [{"kind": action_kind, "selector": action["selector"], "label": action["label"], "authorized": True}], expected_run=run):
                return
            self._set_autopilot_state(
                tab_id, "waiting_for_navigation", f"Queued safe progression in the same tab: {action['label']}.",
                waiting_for_navigation_revision=revision,
                waiting_for_navigation_document_key=str(snapshot.get("documentKey", "")),
                waiting_for_navigation_active_step=inferred_active_step(snapshot),
                waiting_for_navigation_since=time.time(),
            )
        except ValueError as error:
            self._set_autopilot_state(tab_id, "paused", f"Could not queue safe progression: {error}")

    def add_result(self, payload: dict[str, Any]) -> None:
        tab_id = str(payload.get("tabId", ""))[:100]
        results = [result for result in payload.get("results", [])[:100] if isinstance(result, dict)]
        safe = {
            "tabId": tab_id,
            "results": results,
            "updatedAt": time.time(),
        }
        with self._lock:
            pending = self._commands.get(tab_id, [])
            completed_statuses = {"applied", "clicked", "uploaded", "already_attached", "submission_clicked", "already_submitted", "already_attempted",
                                  "blocked", "unsupported", "option_not_found", "stale_document", "invalid_file", "file_type_not_accepted", "upload_failed", "validation_failed"}
            terminal_ids = {
                str(result.get("commandId")) for result in results
                if result.get("commandId") and str(result.get("status", "")) in completed_statuses
            }
            # Older extension versions do not echo commandId. Match a command
            # by its selector once so a successful field action is not replayed.
            for result in results:
                if result.get("commandId") or str(result.get("status", "")) not in completed_statuses:
                    continue
                match = next((command for command in pending if command.get("selector") == result.get("selector")), None)
                if match:
                    terminal_ids.add(str(match["commandId"]))
            if terminal_ids:
                self._commands[tab_id] = [command for command in pending if command["commandId"] not in terminal_ids]
            # Count a safe page step only after the paired tab actually clicked
            # it.  A stale/blocked command must not consume the run's limit.
            if any(str(result.get("status", "")) == "clicked" for result in results):
                run = self._autopilot.get(tab_id)
                if run and run.get("waiting_for_navigation_revision") is not None:
                    run["steps"] = int(run.get("steps", 0)) + 1
                    run["updatedAt"] = time.time()
            self._results.append(safe)
            self._results = self._results[-100:]
            active = self._autopilot.get(tab_id)
            if active:
                active['recent_action_results'] = [
                    {'label': command['label'], 'kind': command['kind'], 'status': result.get('status')}
                    for result in results for command in pending if command['commandId'] == result.get('commandId')]
        failures = [result for result in results if str(result.get("status", "")) in {"blocked", "unsupported", "option_not_found", "stale_document", "invalid_file", "file_type_not_accepted", "upload_failed", "validation_failed"}]
        if failures:
            with self._lock:
                run = self._autopilot.get(tab_id, {})
                self._commands.pop(tab_id, None)
                run["failed_field_selectors"] = [command["selector"] for command in pending
                    if command["kind"] in {"fill", "select", "resume-select"}
                    and any(failure.get("commandId") == command["commandId"] for failure in failures)]
                # Dynamic forms can replace controls between observation and
                # execution. Re-observe a positively rejected stale batch;
                # never replay actions that might already have been sent.
                stale_only = all(failure.get("status") == "stale_document" for failure in failures)
                failed_ids = {failure.get("commandId") for failure in failures}
                failed_commands = [command for command in pending if command["commandId"] in failed_ids]
                recoverable = failed_commands and all(command["kind"] in {
                    "fill", "select", "check", "resume-select", "resume-add", "click"
                } for command in failed_commands)
                retries = run.setdefault("stale_retries", {})
                key = str(self._tabs.get(tab_id, {}).get("documentKey", ""))
                if stale_only and recoverable and retries.get(key, 0) < 2:
                    retries[key] = retries.get(key, 0) + 1
                    for marker in ("waiting_for_navigation_revision", "waiting_for_navigation_document_key",
                                   "waiting_for_navigation_active_step", "waiting_for_navigation_since"):
                        run.pop(marker, None)
                    for command in failed_commands:
                        if command["kind"] == "resume-add":
                            run.get("resume_add_attempts", {}).pop(
                                (command["documentKey"], command["section"], command["rowCount"]), None)
                    run.update(state="waiting_for_snapshot", updatedAt=time.time(),
                               reason="The page changed during the action. Rechecking its current controls.")
                    return
            # A positive refusal is different from a lost acknowledgement: only
            # the former lets a later explicit restart send a fresh attempt.
            if run.get("submission_url") and any(
                command["kind"] == "submit" and command["commandId"] == failure.get("commandId")
                for command in pending for failure in failures
            ):
                from application_tracking import ApplicationTracker
                ApplicationTracker(self._database_path).release_submission(run["submission_url"])
            explanations = {
                "blocked": "The control is unavailable or no longer passes the action checks.",
                "unsupported": "This control needs an adapter; repeating the same action will not fix it.",
                "option_not_found": "No matching dropdown option was found. Select the intended option on the page.",
                "stale_document": "The page kept changing during execution after bounded retries.",
                "invalid_file": "The saved resume could not be read as an upload.",
                "file_type_not_accepted": "This upload control does not accept the saved resume format.",
                "upload_failed": "The browser could not attach the saved resume.",
                "validation_failed": "The employer form rejected validation. Correct the highlighted fields.",
            }
            details = []
            for failure in failures[:3]:
                command = next((item for item in pending if
                    (failure.get("commandId") and item["commandId"] == failure["commandId"])
                    or (not failure.get("commandId") and item["selector"] == failure.get("selector"))), {})
                # Describe the affected control, never echo candidate values,
                # credentials, or file contents into the panel.
                target = str(command.get("label") or "Browser action")[:180]
                status = failure["status"]
                details.append(f"{target} ({status}): {explanations[status]}")
            self._set_autopilot_state(tab_id, "action_failed", " ".join(details) + " Choose Continue after correcting the page.")


def create_bridge_app(bridge: ExtensionBridge | None = None) -> FastAPI:
    if bridge is None:
        from model_application_planner import ModelApplicationPlanner
        bridge = ExtensionBridge(decision_planner=ModelApplicationPlanner())
    app = FastAPI(title="Job Application Chrome Extension Bridge", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(chrome-extension://[a-p]{32}|http://(?:127\.0\.0\.1|localhost)(?::\d+)?)$",
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def local_clients_only(request, call_next):
        # An employer page cannot use the loopback companion to read the CV or
        # start runs. Chrome extension and local dashboard requests are allowed.
        origin = request.headers.get("origin", "")
        if origin and not re.fullmatch(r"chrome-extension://[a-p]{32}|http://(?:127\.0\.0\.1|localhost)(?::\d+)?", origin):
            from starlette.responses import JSONResponse
            return JSONResponse({"detail": "This service is available to the local application extension only."}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "protocol": BRIDGE_PROTOCOL,
                "decisionEngine": "model" if bridge.decision_planner else "local"}

    @app.get("/v1/tabs")
    def tabs():
        return {"tabs": bridge.tabs(), "activeTabId": bridge.active_tab_id()}

    @app.post("/v1/tabs/snapshot")
    def snapshot(payload: dict[str, Any]):
        try:
            if int(payload.get("bridgeProtocol", 0)) != BRIDGE_PROTOCOL:
                raise ValueError("The Chrome extension is out of date. Reload Job Application Copilot Bridge on chrome://extensions.")
            return {"tab": bridge.save_snapshot(payload)}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/v1/commands")
    def queue(payload: dict[str, Any]):
        try:
            count = bridge.queue_commands(str(payload.get("tabId", "")), list(payload.get("commands", [])))
            return {"queued": count}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/commands/{tab_id}")
    def commands(tab_id: str):
        return {"commands": bridge.pending_commands(tab_id)}

    @app.post("/v1/results")
    def result(payload: dict[str, Any]):
        bridge.add_result(payload)
        return {"accepted": True}

    @app.post("/v1/autopilot/{tab_id}/learned-field")
    def learned_field(tab_id: str, payload: dict[str, Any]):
        # The content script sends this only after a trusted manual edit.  The
        # bridge validates it against the paired snapshot and deliberately
        # returns no candidate value.
        bridge.remember_manual_field(tab_id, dict(payload.get("field", {})))
        return {"remembered": True}

    @app.post("/v1/autopilot/{tab_id}")
    def start_autopilot(tab_id: str, payload: dict[str, Any]):
        try:
            profile = dict(payload.get("profile", {}))
            answers = dict(payload.get("answers", {}))
            if not profile:
                saved_profile, saved_answers = load_saved_candidate_context(bridge._database_path)
                profile = saved_profile
                if not answers:
                    answers = saved_answers
            return {"status": bridge.start_autopilot(
                tab_id, profile, answers, int(payload.get("maxSteps", 8)), auto_submit=payload.get("autoSubmit") is True,
                auto_register=payload.get("autoRegister") is True,
            )}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/v1/autopilot/{tab_id}/instruction")
    def instruction(tab_id: str, payload: dict[str, Any]):
        try:
            return bridge.apply_instruction(
                tab_id,
                payload.get("instruction", ""),
                bool(payload.get("remember", False)),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/autopilot/{tab_id}")
    def autopilot_status(tab_id: str):
        return {"status": bridge.autopilot_status(tab_id)}

    @app.delete("/v1/autopilot/{tab_id}")
    def stop_autopilot(tab_id: str):
        bridge.stop_autopilot(tab_id)
        return {"status": bridge.autopilot_status(tab_id)}

    return app


@dataclass
class BridgeServer:
    """A background, loopback-only bridge used by the Streamlit dashboard."""

    bridge: ExtensionBridge | None
    server: uvicorn.Server | None
    thread: threading.Thread | None

    def tabs(self) -> list[dict[str, Any]]:
        if self.bridge:
            return self.bridge.tabs()
        with urlopen("http://127.0.0.1:8765/v1/tabs", timeout=2) as response:
            return list(json.loads(response.read())["tabs"])

    def queue_commands(self, tab_id: str, commands: list[dict[str, Any]]) -> int:
        if self.bridge:
            return self.bridge.queue_commands(tab_id, commands)
        body = json.dumps({"tabId": tab_id, "commands": commands}).encode("utf-8")
        request = Request(
            "http://127.0.0.1:8765/v1/commands", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return int(json.loads(response.read())["queued"])

    def start_autopilot(self, tab_id: str, profile: dict[str, Any], answers: dict[str, Any], max_steps: int = 8) -> dict[str, Any]:
        if self.bridge:
            return self.bridge.start_autopilot(tab_id, profile, answers, max_steps)
        body = json.dumps({"profile": profile, "answers": answers, "maxSteps": max_steps}).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:8765/v1/autopilot/{tab_id}", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return dict(json.loads(response.read())["status"])

    def autopilot_status(self, tab_id: str) -> dict[str, Any]:
        if self.bridge:
            return self.bridge.autopilot_status(tab_id)
        with urlopen(f"http://127.0.0.1:8765/v1/autopilot/{tab_id}", timeout=2) as response:
            return dict(json.loads(response.read())["status"])

    def stop_autopilot(self, tab_id: str) -> None:
        if self.bridge:
            self.bridge.stop_autopilot(tab_id)
            return
        request = Request(f"http://127.0.0.1:8765/v1/autopilot/{tab_id}", method="DELETE")
        with urlopen(request, timeout=2):
            pass


def start_bridge_server(port: int = 8765) -> BridgeServer:
    health = None
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
            health = json.loads(response.read())
    except Exception:
        pass
    if health and health.get("status") == "ok":
        if int(health.get("protocol", 0)) == BRIDGE_PROTOCOL:
            return BridgeServer(bridge=None, server=None, thread=None)
        raise RuntimeError("A stale local application bridge is running. Restart the dashboard, then reload the Chrome extension.")
    from model_application_planner import ModelApplicationPlanner
    bridge = ExtensionBridge(decision_planner=ModelApplicationPlanner())
    config = uvicorn.Config(create_bridge_app(bridge), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="chrome-extension-bridge", daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                if json.loads(response.read())["status"] == "ok":
                    return BridgeServer(bridge=bridge, server=server, thread=thread)
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("The local Chrome extension bridge did not start on port 8765.")
