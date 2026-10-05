"""Known ATS workflow metadata and candidate-controlled employment records.

The browser agent remains deliberately conservative: this module identifies
workflows it understands and validates facts the candidate has explicitly saved.
It never derives employment dates, work-authorisation answers, credentials, or
equality-monitoring answers from a CV or from an AI model.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse


def workflow_for_url(url: str) -> dict[str, Any]:
    """Describe the supported workflow for a job URL without loading it."""
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith("oracleoutsourcing.com"):
        return {
            "id": "oracle_recruiting",
            "name": "Oracle Recruiting",
            "can_attach_cv": True,
            "can_add_employment": True,
            "notes": (
                "The agent can attach the stored CV and add only employment records "
                "you have explicitly saved with exact dates. It pauses for account, "
                "legal, declaration, and final-review steps."
            ),
        }
    if host.endswith("networxrecruitment.com"):
        return {
            "id": "networx",
            "name": "Networx Recruitment",
            "can_attach_cv": True,
            "can_add_employment": False,
            "notes": (
                "The agent can map normal contact and narrative fields. It pauses for "
                "legal declarations, equality monitoring, and final review."
            ),
        }
    return {
        "id": "standard",
        "name": "Standard web form",
        "can_attach_cv": True,
        "can_add_employment": False,
        "notes": "The agent analyses visible standard HTML form controls and stops at unfamiliar workflows.",
    }


def validate_employment_history(records: Any) -> tuple[list[dict[str, str | bool]], list[str]]:
    """Validate user-supplied employment records before an ATS adapter may use them.

    Dates must be exact ISO dates, rather than month-only CV dates. This prevents
    an agent turning an approximate résumé date into an asserted fact on an
    application form.
    """
    if not isinstance(records, list):
        return [], ["Employment history must be a list of records."]

    cleaned: list[dict[str, str | bool]] = []
    problems: list[str] = []
    for position, raw in enumerate(records, start=1):
        if not isinstance(raw, dict):
            problems.append(f"Employment record {position} is invalid.")
            continue
        employer = str(raw.get("employer", "")).strip()
        title = str(raw.get("title", "")).strip()
        start_date = str(raw.get("start_date", "")).strip()
        end_date = str(raw.get("end_date", "")).strip()
        description = str(raw.get("description", "")).strip()
        current = bool(raw.get("current", False))
        missing = [label for label, value in (("employer", employer), ("job title", title), ("exact start date", start_date)) if not value]
        if missing:
            problems.append(f"Employment record {position} is missing {', '.join(missing)}.")
            continue
        if not _is_iso_date(start_date):
            problems.append(f"Employment record {position} needs an exact start date in YYYY-MM-DD format.")
            continue
        if current and end_date:
            problems.append(f"Employment record {position} is marked current but also has an end date.")
            continue
        if not current:
            if not end_date:
                problems.append(f"Employment record {position} needs an exact end date or must be marked current.")
                continue
            if not _is_iso_date(end_date):
                problems.append(f"Employment record {position} needs an exact end date in YYYY-MM-DD format.")
                continue
            if end_date < start_date:
                problems.append(f"Employment record {position} ends before it starts.")
                continue
        cleaned.append({
            "employer": employer,
            "title": title,
            "start_date": start_date,
            "end_date": "" if current else end_date,
            "current": current,
            "description": description,
        })
    return cleaned, problems


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False
