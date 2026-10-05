"""Explicit CV education/certifications for Workday repeating sections."""
import re
import hashlib
import json
from urllib.parse import urlsplit
from application_narratives import resume_text


def degree_memory_key(url, record):
    identity = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return 'education-degree:' + (urlsplit(url).hostname or '').casefold() + ':' + hashlib.sha256(identity.encode()).hexdigest()


def education_selection_key(url, resume):
    return 'education-selection:' + degree_memory_key(url, qualifications(resume)['education'])


def instruction_degree_key(snapshot, field, resume):
    """Bind corrections to a uniquely identified resume record, never row IDs."""
    if field.get('resumeSection') != 'education' or not re.fullmatch(r'degree\W*', field.get('label', ''), re.I):
        return None
    schools = [f.get('value', '').strip().casefold() for f in snapshot.get('fields', [])
               if f.get('resumeRow') == field.get('resumeRow')
               and re.search(r'school|university|institution', f.get('label', ''), re.I) and f.get('value')]
    matches = [r for r in qualifications(resume)['education']
               if any(s in {r['school'].casefold(), r['school'].split(',')[0].casefold()} for s in schools)]
    return degree_memory_key(snapshot['url'], matches[0]) if len(matches) == 1 else None


def qualifications(resume):
    records = {"education": [], "certifications": []}
    section = None
    for line in resume_text(resume).splitlines():
        line = line.strip()
        if not line:
            continue
        heading = line.casefold().rstrip(":")
        if heading in {"education", "postgraduate education", "academic qualifications"}:
            section = "education"
            continue
        if heading in {"certifications", "professional certifications"}:
            section = "certifications"
            continue
        if line.isupper() and "|" not in line:
            section = None
        if section == "certifications":
            name = line.lstrip("•- ")
            if name and not any(r["name"] == name for r in records[section]):
                records[section].append({"name": name})
        elif section == "education":
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 2:
                continue
            degree, school = parts[:2]
            study = re.search(r"\((?!PGP\))([^()]+)\)", degree, re.I)
            subject = study[1] if study else (degree.split(" in ", 1)[1] if " in " in degree else "")
            credential = degree[:study.start()].strip() if study else degree
            years = re.findall(r"\b(?:19|20)\d{2}\b", " | ".join(parts[2:]))
            date_range = re.search(r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})\b", " | ".join(parts[2:]))
            gpa = re.search(r"\bGPA\s*[:=]?\s*(\d+(?:\.\d+)?)", " | ".join(parts[2:]), re.I)
            record = {"degree": credential, "school": school, "study": subject,
                      "end_year": years[-1] if years else "", "start_year": date_range[1] if date_range else "",
                      "gpa": gpa[1] if gpa else ""}
            if record not in records[section]:
                records[section].append(record)
    return records


def qualification_plan(snapshot, resume, answers=None, selected_education=None):
    result = {"values": {}, "add": None, "problems": []}
    if not (urlsplit(snapshot.get("url", "")).hostname or "").endswith(".myworkdayjobs.com"):
        return result
    records = qualifications(resume)
    if selected_education is not None:
        records['education'] = selected_education
    from workday_employment import resume_employment
    records["employment"] = resume_employment(resume)
    for section in snapshot.get("resumeSections", []):
        kind = section["kind"]
        source = records.get(kind, [])
        if not source:
            continue
        groups = {}
        for field in snapshot.get("fields", []):
            if field.get("resumeSection") == kind and field.get("resumeRow"):
                groups.setdefault(field["resumeRow"], []).append(field)
        for index, fields in enumerate(groups.values()):
            if kind == "employment":
                break  # Employment values use the Workday field-ID mapper.
            if index >= len(source):
                if kind == 'education' and selected_education is not None:
                    result['problems'].append('Remove the extra education row on the page; your instruction selects fewer records.')
                break
            record = source[index]
            # Retain existing user data. Resolve a prefilled institution or
            # certification to the corresponding CV record where possible.
            identity = next((f for f in fields if re.search(r"school|university|institution|certification", f["label"], re.I)
                             and not re.search(r"number|date|id\b", f["label"], re.I) and f.get("value")), None)
            replacing = kind == 'education' and selected_education is not None
            if identity and identity["value"].casefold() != "other" and not replacing:
                key = "school" if kind == "education" else "name"
                matches = [r for r in source if identity["value"].casefold() in {r[key].casefold(), r[key].split(",")[0].casefold()}]
                if len(matches) == 1:
                    record = matches[0]
                else:
                    result["problems"].append(f"Review the existing {kind} record; it does not match a CV entry.")
                    continue
            for field in fields:
                if field.get("value") and not replacing:
                    continue
                label = field["label"].casefold()
                value = ""
                if kind == "education":
                    if re.search(r"school|university|institution", label): value = record["school"]
                    elif "field of study" in label or "major" in label: value = record["study"]
                    elif "degree" in label:
                        saved = (answers or {}).get(degree_memory_key(snapshot['url'], record))
                        value = record["degree"] if replacing else saved or record["degree"]
                    elif "gpa" in label or "grade point" in label:
                        value = record["gpa"]
                        if not value and re.search(r'write\s*["\u201c]?NA["\u201d]?\s*if GPA is not available', snapshot.get("body", ""), re.I): value = "NA"
                    elif re.search(r"(to|end|graduat|complet).*year|year.*(graduat|complet)", label) or re.search(r"(lastYear|endDate).*Year|lastYearAttended", field["selector"], re.I): value = record["end_year"]
                    elif re.search(r"(from|start).*year", label) or "firstYearAttended" in field["selector"]: value = record["start_year"]
                elif re.search(r"certification|certificate|credential|name", label) and not re.search(r"number|date|id\b|issuer|issued|issuing", label):
                    value = record["name"]
                if value:
                    if field.get('value', '').strip().casefold() == value.strip().casefold():
                        continue
                    aliases = [value]
                    if kind == "education" and value == record["school"] and "," in value:
                        aliases.append(value.split(",")[0].strip())
                    if value == "Bachelor of Engineering": aliases += ["Bachelor of Engineering (BEng)", "BEng", "Bachelor's Degree"]
                    result["values"][field["selector"]] = {"value": value, "aliases": aliases}
                elif field.get("required") or (replacing and field.get('value')):
                    result["problems"].append(f"{field['label']} is required but is not stated in the CV.")
        if len(groups) < len(source) and section.get("add") and result["add"] is None:
            result["add"] = {**section["add"], "section": kind, "rowCount": len(groups)}
        elif not groups and not section.get("add"):
            result["problems"].append(f"Open the {kind} section so its CV records can be filled.")
    return result
