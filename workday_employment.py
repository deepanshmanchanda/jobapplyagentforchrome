"""Map explicit dated CV employment records to visible Workday work rows."""
import calendar
import re
from urllib.parse import urlsplit
from application_narratives import resume_text

MONTHS = {name.casefold(): str(i) for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.casefold(): str(i) for i, name in enumerate(calendar.month_abbr) if name})
DATE = r"([A-Za-z]+)\s+(\d{4})\s*[-–—]\s*(?:(present|current)|([A-Za-z]+)\s+(\d{4}))"


def resume_employment(resume):
    lines = [line.strip() for line in resume_text(resume).splitlines() if line.strip()]
    records = []
    active = False
    for index, line in enumerate(lines):
        if line.casefold() in {"professional experience", "work experience", "employment history"}:
            active = True
            continue
        if line.casefold() in {"education", "certifications", "selected impact and achievements", "core skills"}:
            active = False
        if not active or "|" not in line or index == 0:
            continue
        title, dates = line.rsplit("|", 1)
        match = re.fullmatch(DATE, dates.strip(), re.I)
        if not match or match[1].casefold() not in MONTHS:
            continue
        if not match[3] and match[4].casefold() not in MONTHS:
            continue
        records.append({"title": title.strip(), "company": lines[index - 1],
                        "start_month": MONTHS[match[1].casefold()], "start_year": match[2],
                        "current": bool(match[3]), "end_month": "" if match[3] else MONTHS[match[4].casefold()],
                        "end_year": "" if match[3] else match[5]})
    return records


def workday_employment_values(snapshot, resume, profile):
    host = urlsplit(snapshot.get("url", "")).hostname or ""
    if not host.endswith(".myworkdayjobs.com"):
        return {}
    records = resume_employment(resume)
    if not records:
        return {}
    groups = {}
    for field in snapshot.get("fields", []):
        match = re.fullmatch(r"(#workExperience-[\w-]+?)--(.+)", field.get("selector", ""))
        if match:
            groups.setdefault(match[1], {})[match[2]] = field
    result = {}
    personal_name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip().casefold()
    used = set()
    # Reserve existing rows first so a newly opened row gets the next record.
    for group in groups.values():
        company = group.get("companyName", {}).get("value", "").strip().casefold()
        title = group.get("jobTitle", {}).get("value", "").strip().casefold()
        matches = [i for i, r in enumerate(records) if r["company"].casefold() == company
                   and (not title or r["title"].casefold() == title)]
        if len(matches) == 1:
            used.add(matches[0])
    for group in groups.values():
        company = group.get("companyName", {}).get("value", "").strip()
        # Correct the earlier generic name-matching bug only when it is
        # exactly this candidate's full name; preserve other employer values.
        missing_company = not company or company.casefold() == personal_name
        title = group.get("jobTitle", {}).get("value", "").strip().casefold()
        matches = [r for r in records if r["company"].casefold() == company.casefold()
                   and (not title or r["title"].casefold() == title)]
        if not matches and missing_company:
            available = [(i, r) for i, r in enumerate(records) if i not in used
                         and (not title or r["title"].casefold() == title)]
            if available:
                index, record = available[0]
                used.add(index)
                matches = [record]
        if len(matches) != 1:
            continue
        record = matches[0]
        values = {"jobTitle": record["title"], "companyName": record["company"],
                  "currentlyWorkHere": "true" if record["current"] else "false",
                  "startDate-dateSectionMonth-input": record["start_month"],
                  "startDate-dateSectionYear-input": record["start_year"],
                  "endDate-dateSectionMonth-input": record["end_month"],
                  "endDate-dateSectionYear-input": record["end_year"]}
        for key, value in values.items():
            field = group.get(key)
            if field and value and (not field.get("value") or (key == "companyName" and missing_company)):
                if key == "currentlyWorkHere" and value == "false":
                    continue
                result[field["selector"]] = value
    return result
