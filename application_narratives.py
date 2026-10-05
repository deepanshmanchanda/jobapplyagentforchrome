"""Draft ordinary application prose from the candidate's saved career evidence.

This local fallback does not need an API key. It composes motivation and fit
answers; it does not guess qualifications, legal facts, or personal anecdotes.
The current page supplies the employer, never an old application's motivation.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
import re
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


# Labels are deliberately bounded: a free-text field alone is not permission to
# answer an unrelated screening question with generic application prose.
_ANECDOTE = re.compile(
    r"\b(a time|an occasion|personal example|give (?:us )?an example|describe an example|"
    r"tell (?:us|me) about a situation|challenge you (?:faced|overcame)|"
    r"biggest achievement|greatest achievement|proudest|received amazing service|"
    r"how did you|when did you|have you ever|why (?:did|were|have)|"
    r"leaving|leave (?:your|the|my) (?:current|last|previous)|fired|dismissed|laid off)\b", re.I,
)
_FACTUAL = re.compile(
    r"\b(password|passcode|verification|captcha|otp|visa|sponsorship|citizenship|"
    r"immigration|eligibility|eligible|work authori[sz]ation|right to work|salary|"
    r"compensation|notice period|start date|date of birth|disability|ethnicity|"
    r"gender|religion|criminal|conviction|national insurance|social security|"
    r"years? of experience|how many years|certification|certified|clearance)\b", re.I,
)
_ROLE_WORD = re.compile(
    r"\b(analyst|engineer|developer|manager|lead|director|specialist|consultant|"
    r"scientist|designer|administrator|officer|associate|assistant|architect|"
    r"coordinator|technician|accountant|representative|advisor|adviser|intern)\b", re.I,
)
_SKILLS = (
    "SQL", "Power BI", "DAX", "Tableau", "Python", "Excel", "VBA", "Azure",
    "data analytics", "business intelligence", "data modelling", "data modeling",
    "data quality", "data analysis", "data engineering", "data visualisation",
    "data visualization", "reporting automation", "process improvement",
    "stakeholder management", "requirements gathering", "project management",
    "risk analysis", "risk management", "financial analysis", "customer service",
    "customer support", "account management", "product management", "JavaScript",
    "TypeScript", "Java", "C++", "C#", "React", "Node.js", "AWS", "Docker",
    "Kubernetes", "machine learning", "statistical analysis", "ETL", "testing",
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _has_term(text: str, term: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I))


def _positive_term(text: str, term: str) -> bool:
    """Do not promote explicit missing experience to a candidate skill."""
    for match in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I):
        prefix = text[max(0, match.start() - 80):match.start()]
        if not re.search(r"\b(no|not|without|lack|lacking|unfamiliar)\b[^.!?;\n]{0,65}$", prefix, re.I):
            return True
    return False


def resume_text(resume: dict[str, Any] | str | None) -> str:
    """Read an uploaded DOCX, PDF, or text CV without writing it to disk."""
    if isinstance(resume, str):
        return resume[:200_000]
    if not isinstance(resume, dict):
        return ""
    for key in ("text", "resume_text", "extracted_text"):
        if isinstance(resume.get(key), str) and resume[key].strip():
            return resume[key][:200_000]
    data = resume.get("data", b"")
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > 20_000_000:
        return ""
    return _extract_resume_text(
        bytes(data), str(resume.get("mime_type", "")).lower(), str(resume.get("name", "")).lower(),
    )


@lru_cache(maxsize=4)
def _extract_resume_text(data: bytes, mime: str, name: str) -> str:
    try:
        if name.endswith(".docx") or "wordprocessingml" in mime:
            with ZipFile(BytesIO(data)) as document:
                part = document.getinfo("word/document.xml")
                if part.file_size > 5_000_000:
                    return ""
                root = ElementTree.fromstring(document.read(part))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            return "\n".join(
                "".join(paragraph.itertext())
                for paragraph in root.findall(".//w:p", ns)
            )[:200_000]
        if name.endswith(".pdf") or mime == "application/pdf":
            if b"%PDF-" not in data[:1024]:
                return ""
            # Optional import keeps contact/form filling functional even when a
            # PDF parser is unavailable; never decode PDF bytes as fake prose.
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages[:30])[:200_000]
        if mime.startswith("text/") or name.endswith((".txt", ".md")):
            return data.decode("utf-8", errors="replace")[:200_000]
    except (ImportError, OSError, ValueError, KeyError, BadZipFile, ElementTree.ParseError):
        return ""
    except Exception:
        # Parser-specific corrupt/encrypted document exceptions must not stop
        # the rest of an application from being filled.
        return ""
    return ""


def _library_answer(answers: dict, key: str) -> str:
    value = answers.get(key, "")
    if isinstance(value, dict):
        if not value.get("reviewed_by_user", value.get("reviewed", False)):
            return ""
        return _clean(value.get("response", ""))
    # String entries are the existing bridge's already-reviewed library shape.
    return _clean(value) if isinstance(value, str) else ""


def _career_evidence(profile: dict, answers: dict, resume: dict | str | None) -> tuple[list[str], list[str], str]:
    skills: list[str] = []
    sources: list[str] = []
    saved_skills = profile.get("skills", [])
    if isinstance(saved_skills, str):
        saved_skills = re.split(r"[,;\n]", saved_skills)
    if isinstance(saved_skills, list):
        skills.extend(_clean(skill) for skill in saved_skills if isinstance(skill, str) and 1 < len(_clean(skill)) < 80)
        if skills:
            sources.append("profile.skills")
    texts = [
        ("profile.summary", _clean(profile.get("summary", profile.get("professional_summary", "")))),
        ("resume", resume_text(resume) or str(profile.get("resume_text", ""))),
        ("answer_library.skills", _library_answer(answers, "skills")),
    ]
    history = profile.get("employment_history", [])
    history = [record for record in history if isinstance(record, dict)] if isinstance(history, list) else []
    for record in history:
        texts.append(("profile.employment_history", _clean(record.get("description", ""))))
    for source, text in texts:
        found = [skill for skill in _SKILLS if _positive_term(text, skill)]
        if found:
            skills.extend(found)
            sources.append(source)
    experience = ""
    for record in sorted(history, key=lambda item: bool(item.get("current")), reverse=True):
        title = _clean(record.get("title", ""))
        employer = _clean(record.get("employer", ""))
        if title:
            experience = f"work as {title}" + (f" at {employer}" if employer else "")
            sources.append("profile.employment_history")
            break
    unique = {skill.casefold(): skill for skill in reversed(skills)}
    return list(reversed(list(unique.values()))), list(dict.fromkeys(sources)), experience


def _employer_name(text: str) -> str:
    candidate = re.split(r"[?!\n|]|\s+(?:because|and (?:why|what|how)|please)\b", text, maxsplit=1, flags=re.I)[0]
    candidate = candidate.strip(" .:*-–—\"'")
    if not candidate or len(candidate) > 90 or len(candidate.split()) > 10:
        return ""
    if candidate.casefold() in {"us", "our company", "our organisation", "our organization", "our team", "this company", "the company", "this role", "this position", "this job"}:
        return ""
    return candidate


def _page_context(snapshot: dict, question: str, answers: dict) -> tuple[str, str]:
    company = ""
    for pattern in (
        r"\b(?:working|work) (?:at|for|with)\s+(.+)",
        r"\b(?:join|joining)\s+(.+)",
        r"\b(?:role|position|job) (?:at|with)\s+(.+)",
        r"\b(?:attracts?|draws?) you to\s+(.+)",
        r"^why\s+([^?]+)\??$",
    ):
        match = re.search(pattern, question, re.I)
        if match:
            company = _employer_name(match.group(1))
            if company and not re.match(r"(?:are|do|would|should|is|have|were)\b", company, re.I):
                break
            company = ""
    job = snapshot.get("job", {})
    job = job if isinstance(job, dict) else {}
    company = company or _clean(snapshot.get("company", job.get("company", "")))
    role = _clean(snapshot.get("role", job.get("role", "")))
    title = _clean(snapshot.get("title", ""))
    at_match = re.search(r"^(.+?)\s+at\s+(.+?)(?:\s*[|–—]\s*.*)?$", title, re.I)
    if at_match and _ROLE_WORD.search(at_match.group(1)):
        role = role or at_match.group(1)
        company = company or _employer_name(at_match.group(2))
    title_parts = [part.strip() for part in re.split(r"\s+[|–—-]\s+", title)]
    for part in title_parts:
        if _ROLE_WORD.search(part) and not role:
            role = part
    if role and not company:
        other = [part for part in title_parts if part != role and part.casefold() not in {"apply", "application", "careers", "jobs", "lever", "greenhouse", "ashby"}]
        if len(other) == 1:
            company = _employer_name(other[0])
    if not company:
        match = re.search(r"(?:^|\n)\s*(?:company|employer)\s*:\s*([^\n]+)", str(snapshot.get("body", "")), re.I)
        company = _employer_name(match.group(1)) if match else ""
    # Only reuse manually supplied context when the current page offers no
    # conflicting employer. An old RHP application must not turn Kraken into RHP.
    if not company:
        company = _clean(answers.get("company", ""))
    if not role and (not answers.get("company") or _clean(answers["company"]).casefold() == company.casefold()):
        role = _clean(answers.get("role", ""))
    role = re.sub(r"^(?:application (?:for|to)|apply (?:for|to)|job (?:opening|application)[: ]*)\s*", "", role, flags=re.I).strip()
    return company, role


def _kind(label: str) -> str:
    if _ANECDOTE.search(label) or _FACTUAL.search(label):
        return ""
    normalized = label.casefold()
    short_why = re.fullmatch(r"why\s+([\w &.'’-]{2,70})\??\s*\*?", normalized)
    employer_why = bool(short_why and not re.match(
        r"(?:are|do|does|did|would|should|is|have|were|was|can|could|will|not|you)\b",
        short_why.group(1),
    ))
    if (
        re.search(r"\bwhy\b.*\b(interested|work|working|join|joining|apply|applying|want|like)\b", normalized)
        or re.search(r"\bwhat\b.*\b(attracts|interests|excites|motivates|draws)\b", normalized)
        or re.search(r"\bmotivation (?:for|to)\b", normalized)
        or employer_why
    ):
        return "motivation"
    if re.search(r"\b(cover letter|covering letter|supporting statement|tell us about yourself|professional summary)\b", normalized):
        return "fit"
    if re.search(r"\b(?:why|what|how)\b.*\b(?:good fit|great fit|strong fit|suitable|suitability|bring to|contribute)\b", normalized):
        return "fit"
    if "skills" in normalized and ("experience" in normalized or "relevant" in normalized):
        return "fit"
    return ""


def _join(items: list[str]) -> str:
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else items[0]


def draft_application_narratives(
    snapshot: dict[str, Any], profile: dict[str, Any], answers: dict[str, Any],
    resume: dict[str, Any] | str | None = None,
) -> dict[str, dict[str, str]]:
    """Return selector-keyed prose and its evidence, ready for the normal plan.

    Existing values, explicit overrides, and remembered exact answers should
    retain precedence in the caller. Empty/missing evidence returns no draft.
    """
    skills, sources, experience = _career_evidence(profile, answers, resume)
    if not skills and not experience:
        return {}
    context = " ".join(str(snapshot.get(key, "")) for key in ("title", "body", "job_description"))
    skills = sorted(skills, key=lambda skill: not _has_term(context, skill))[:5]
    evidence = _join(skills) if skills else experience
    results = {}
    for field in snapshot.get("fields", []):
        control = str(field.get("type", field.get("control", ""))).casefold()
        selector = str(field.get("selector", ""))
        label = _clean(field.get("label", field.get("field", "")))
        if not selector or control not in {"text", "textarea", ""} or field.get("value"):
            continue
        kind = _kind(label)
        if not kind:
            continue
        company, role = _page_context(snapshot, label, answers)
        target = f"the {role} role" if role else "this role"
        if company:
            target += f" at {company}"
        if kind == "motivation":
            value = f"I am interested in {target} because it would give me an opportunity to apply my background in {evidence}."
        else:
            value = f"I would bring a background in {evidence} to {target}."
        if skills and experience:
            value += f" My background also includes {experience}."
        if any(_has_term(" ".join(skills), term) for term in ("SQL", "Power BI", "Tableau", "data analytics", "data analysis", "business intelligence")):
            value += " I would use those skills to turn complex data into clear reporting and practical decisions."
        elif any(_has_term(" ".join(skills), term) for term in ("Python", "Java", "JavaScript", "TypeScript", "C++", "React")):
            value += " I would apply those skills to building reliable solutions and improving how work gets done."
        team = f"{company}'s team" if company else "the team"
        value += f" I would welcome the opportunity to understand the priorities of {team} for this position and contribute useful, dependable work."
        # Keep complete sentences if the page exposes a character limit.
        try:
            limit = int(field.get("maxLength", field.get("maxlength", 0)) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0 and len(value) > limit:
            sentences = re.split(r"(?<=[.!?])\s+", value)
            value = ""
            for sentence in sentences:
                proposed = (value + " " + sentence).strip()
                if len(proposed) > limit:
                    break
                value = proposed
        if value:
            results[selector] = {"value": value, "source": "Drafted from " + ", ".join(sources)}
    return results
