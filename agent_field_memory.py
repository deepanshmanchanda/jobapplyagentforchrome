"""Local, reusable answers for ordinary application form fields.

This module is intentionally small and deterministic.  It gives the browser
agent a way to remember a candidate's *own* ordinary text-field answer for
the same question on the same employer site, without turning a free-text
instruction into an unrestricted browser command.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse


# These categories must remain direct candidate actions even when the
# candidate asks the side-panel agent to remember an answer.  They cover
# credentials, account/security controls, legally consequential declarations,
# protected-characteristic monitoring, and third-party reference details.
MANUAL_ONLY_TERMS = (
    "password", "passcode", "one time", "otp", "verification", "captcha",
    "sign in", "log in", "login", "create account", "user name", "username",
    "submit", "send application", "complete application", "final submit",
    "national insurance", "social security",
    "legally entitled", "right to work", "work authorisation", "work authorization",
    "immigration", "visa", "sponsorship", "sponsor", "citizenship",
    "country of application", "country region code",
    "criminal conviction", "criminal record", "conviction",
    "ethnic", "ethnicity", "race", "religion", "disability", "reasonable adjustment",
    "gender", "sex", "sexual orientation", "marital", "marriage", "civil partnership",
    "date of birth", "birth date", "age", "veteran", "military service",
    "referee", "reference contact", "reference details",
    "salary", "compensation", "bank account", "tax code",
)

SAFE_TEXT_CONTROLS = {"text", "textarea", "email", "tel", "url", "search", "number", "select-one", "select", "combobox"}


def normalise_field_label(value: Any) -> str:
    """Produce a stable key for a visible form label.

    Required asterisks and punctuation often vary across a site, while the
    actual visible question remains the same.  The key deliberately does not
    include values or a full URL path.
    """
    text = str(value or "")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).casefold()
    return " ".join(text.split())


def is_manual_only_field(label: Any) -> bool:
    normalized = normalise_field_label(label)
    return not normalized or any(term in normalized for term in MANUAL_ONLY_TERMS)


def is_reusable_safe_field(label: Any, control: Any) -> bool:
    normalized = normalise_field_label(label)
    return (
        str(control or "").casefold() in SAFE_TEXT_CONTROLS
        and not is_manual_only_field(normalized)
        and not re.fullmatch(r"wd[0-9a-f]+", normalized, re.I)
        and normalized not in {"unlabelled field", "unlabeled field"}
    )


def reusable_field_answer_key(page_url: str, label: Any, control: Any) -> str:
    """Return a site-scoped, exact-question key for an ordinary field."""
    host = (urlparse(str(page_url or "")).hostname or "").casefold()
    normalized_label = normalise_field_label(label)
    normalized_control = str(control or "").casefold()
    material = f"{host}\x1f{normalized_label}\x1f{normalized_control}".encode("utf-8")
    return f"safe-field:{host}:{hashlib.sha256(material).hexdigest()}"


def remembered_field_answer(
    page_url: str, label: Any, control: Any, answers: dict[str, Any],
) -> str | None:
    """Return a remembered answer only for a safe exact field/site match."""
    if not is_reusable_safe_field(label, control):
        return None
    value = answers.get(reusable_field_answer_key(page_url, label, control))
    value = str(value or "").strip()
    return value or None


def parse_agent_instruction(text: Any) -> tuple[str, str | None, str | None]:
    """Parse the small, explicit instruction language used by the side panel.

    Supported forms are ``Continue`` (or a sentence containing continue,
    resume, retry, or proceed) and ``Answer Field label: value``.  Bare
    ``Field label: value`` is accepted too, so candidates can copy a visible
    label and type a response with minimal ceremony.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("Type Continue, or Answer [exact visible field label]: [answer].")
    if len(raw) > 4_000:
        raise ValueError("Keep one instruction under 4,000 characters.")
    lower = raw.casefold()
    if any(term in lower for term in ("password", "passcode", "one time", "otp", "verification", "captcha")):
        raise ValueError("Do not put passwords, codes, verification details, or CAPTCHA text in the agent instruction box.")
    if re.fullmatch(r"(?:please\s+)?(?:continue|resume|retry|proceed)[.!]?", lower):
        return "continue", None, None
    if re.search(r"\b(do not|don't|dont|instead|only|just|add|skip|exclude|include|remove|delete|replace|click|navigate|submit|upload)\b", lower):
        return "workflow", None, raw

    match = re.fullmatch(r"\s*(?:answer|fill)\s+(.+?)\s*:\s*(.+)\s*", raw, flags=re.I | re.S)
    if not match:
        match = re.fullmatch(r"\s*(.+?)\s*:\s*(.+)\s*", raw, flags=re.S)
    if not match:
        if re.search(r"\b(click|navigate|delete|remove|submit|upload|add another)\b", lower):
            raise ValueError("This box can answer a field or continue the run. That browser procedure is not supported as a text instruction yet.")
        value = re.sub(r"^(?:it['’]?s|it is|select|choose|use)\s+", "", raw, flags=re.I).strip()
        value = re.sub(r"^(?:a|an)\s+(?=(?:technical|high school|college|postgraduate|diploma|bachelor|master|associate|doctorate)\b)", "", value, flags=re.I)
        return "context_answer", None, value
    label, value = match.group(1).strip(), match.group(2).strip()
    if not label or not value:
        raise ValueError("Use Answer [exact visible field label]: [answer].")
    if is_manual_only_field(label):
        raise ValueError("That field must be completed directly in the application page.")
    return "answer", label, value
