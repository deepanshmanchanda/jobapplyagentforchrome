"""Local, auditable job intake and review pipeline.

This module classifies jobs and records review state. It deliberately has no
application-submission function; auto-submit is a dry-run eligibility tier.
"""

from __future__ import annotations

import concurrent.futures
import csv
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import yaml


DEFAULT_CONFIG = {
    "auto_apply_enabled": False,
    "thresholds": {"minimum_match_score": 75, "auto_submit_match_score": 85},
    "limits": {"daily_volume": 70, "max_concurrency": 2},
    "scoring": {"provider": "offline_keyword_overlap", "skills_weight": 70, "title_weight": 30},
    "risk_rules": {
        "require_easy_apply_for_auto_tier": True,
        "require_answerable_screening_for_auto_tier": True,
        "require_configured_sponsorship_constraint_for_auto_tier": True,
        "require_exact_matches": ["location_match", "seniority_match", "visa_match"],
        "reject_unanswerable_custom_ats": True,
        "reject_no_sponsorship_when_required": True,
    },
    "constraints": {
        "location": "United Kingdom",
        "seniority": "Senior",
        "require_sponsorship": None,
    },
    "profile": {
        "target_titles": ["Data Analyst", "Senior Data Analyst", "BI Analyst"],
        "skills": [
            "SQL", "SQL Server", "Python", "Power BI", "DAX", "Tableau", "Excel", "VBA",
            "MS Access", "Azure", "Azure Functions", "Logic Apps", "APIs", "Data Modelling",
            "Data Visualisation", "Data Quality", "Process Automation", "Stakeholder Engagement",
            "UAT", "SAP", "Oracle", "RAG", "GenAI API", "LangGraph", "CrewAI",
            "Risk Analytics", "Audit Analytics", "Process Analytics",
        ],
        "skill_catalog": [
            "SQL", "SQL Server", "Python", "R", "Power BI", "DAX", "Tableau", "Excel", "VBA",
            "MS Access", "Azure", "AWS", "Azure Functions", "Logic Apps", "APIs", "Data Modelling",
            "Data Visualisation", "Data Quality", "ETL", "Power Query", "Snowflake", "Databricks",
            "dbt", "Looker", "Qlik", "SSIS", "SSRS", "Statistics", "Machine Learning", "GenAI",
            "RAG", "LangGraph", "CrewAI", "Process Automation", "Stakeholder Engagement", "UAT",
            "Requirements Gathering", "SAP", "Oracle", "Risk Analytics", "Audit Analytics", "Process Analytics",
        ],
        "screening_answers": {
            "right_to_work": "",
            "visa_status": "",
            "requires_sponsorship": "",
            "notice_period": "",
            "salary_expectation": "",
        },
    },
    "safety": {"pause_after_platform_errors": 3},
}

VALID_FLOWS = {"easy_apply", "custom_ats", "unknown"}
VALID_SPONSORSHIP_STATUSES = {"unknown", "evidence_found", "no_sponsorship"}
VALID_OUTCOMES = {"unknown", "rejected", "interview", "offer", "hired", "ghosted"}
RESPONSE_OUTCOMES = {"rejected", "interview", "offer", "hired"}
TRACKING_PARAMETERS = {"trk", "ref", "source", "from", "campaignid"}


def load_config(path: str | Path = "config.yaml") -> dict:
    """Load YAML config, merging user values over conservative defaults."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config_path = Path(path)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as config_file:
            supplied = yaml.safe_load(config_file) or {}
        _deep_merge(config, supplied)
    return config


def _deep_merge(base: dict, supplied: dict) -> None:
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def default_search_preferences(config: dict) -> dict:
    """Return editable search preferences without changing the YAML baseline."""
    constraints = config.get("constraints", {})
    profile = config.get("profile", {})
    return {
        "redundancy_target_date": "",
        "work_locations": [constraints.get("location", "")],
        "salary_minimum": "",
        "salary_maximum": "",
        "require_sponsorship": constraints.get("require_sponsorship"),
        "visa_constraint_note": "",
        "target_titles": list(profile.get("target_titles", [])),
    }


def apply_search_preferences(config: dict, preferences: dict) -> dict:
    """Overlay saved candidate choices onto a runtime config used for ranking."""
    effective = json.loads(json.dumps(config))
    locations = [item.strip() for item in preferences.get("work_locations", []) if str(item).strip()]
    titles = [item.strip() for item in preferences.get("target_titles", []) if str(item).strip()]
    if locations:
        effective["constraints"]["location"] = ", ".join(locations)
    if titles:
        effective["profile"]["target_titles"] = titles
    if preferences.get("require_sponsorship") in {True, False, None}:
        effective["constraints"]["require_sponsorship"] = preferences["require_sponsorship"]
    return effective


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Job URL must be an absolute HTTP or HTTPS URL")
    host = parsed.hostname.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(sorted(query)), ""))


def normalize_job(raw_job: dict) -> dict:
    job = {key: (value.strip() if isinstance(value, str) else value) for key, value in raw_job.items()}
    if not job.get("url"):
        raise ValueError("Each job must include a URL")
    job["url"] = canonicalize_url(job["url"])
    job.setdefault("title", "")
    job.setdefault("company", "")
    job.setdefault("location", "")
    job.setdefault("description", "")
    job.setdefault("application_flow_type", "unknown")
    if job["application_flow_type"] not in VALID_FLOWS:
        job["application_flow_type"] = "unknown"
    for key in ["screening_questions_answerable", "location_match", "seniority_match", "visa_match", "explicit_no_sponsorship"]:
        if key in job and isinstance(job[key], str):
            job[key] = job[key].strip().lower() in {"true", "yes", "1", "match", "answerable"}
    job.setdefault("screening_questions_answerable", False)
    job.setdefault("location_match", False)
    job.setdefault("seniority_match", False)
    job.setdefault("visa_match", False)
    job.setdefault("explicit_no_sponsorship", False)
    sponsorship_status = str(job.get("sponsorship_status", "")).strip().lower()
    if not sponsorship_status:
        sponsorship_status = "no_sponsorship" if job["explicit_no_sponsorship"] else "unknown"
    if sponsorship_status not in VALID_SPONSORSHIP_STATUSES:
        sponsorship_status = "unknown"
    job["sponsorship_status"] = sponsorship_status
    job["explicit_no_sponsorship"] = sponsorship_status == "no_sponsorship"
    job.setdefault("sponsorship_evidence", "")
    job.setdefault("sponsorship_provenance", "")
    return job


def score_job(job: dict, config: dict) -> dict:
    """Return a transparent offline 0-100 skill/title overlap score."""
    title = str(job.get("title", ""))
    description = str(job.get("description", ""))
    searchable = f"{title} {description}".casefold()
    skills = list(config["profile"].get("skills", []))
    target_titles = list(config["profile"].get("target_titles", []))
    skill_hits = [skill for skill in skills if skill.casefold() in searchable]
    title_hits = [target for target in target_titles if target.casefold() in title.casefold()]
    scoring = config.get("scoring", DEFAULT_CONFIG["scoring"])
    skill_weight = float(scoring["skills_weight"])
    title_weight = float(scoring["title_weight"])
    total_weight = max(skill_weight + title_weight, 1)
    skill_score = total_weight * (skill_weight / total_weight) * len(skill_hits) / max(len(skills), 1)
    title_score = total_weight * (title_weight / total_weight) if title_hits else 0
    score = round(min(100, skill_score + title_score))
    missing = [skill for skill in skills if skill not in skill_hits]
    reason = (
        f"Matched {len(skill_hits)} of {len(skills)} configured skills"
        + (f" and target title '{title_hits[0]}'." if title_hits else "; no configured target title matched.")
    )
    return {
        "score": score,
        "reason": reason,
        "missing_keywords": missing,
        "matched_keywords": skill_hits + title_hits,
        "method": "offline_keyword_overlap",
        "skill_gap": analyze_skill_gap(description, config["profile"]),
    }


def analyze_skill_gap(description: str, profile: dict) -> dict:
    """Compare resume-backed skills with catalogued skills explicitly named in a role description."""
    description = description.casefold()
    candidate = list(profile.get("skills", []))
    catalog = list(profile.get("skill_catalog", DEFAULT_CONFIG["profile"]["skill_catalog"]))
    required = [
        skill
        for skill in catalog
        if re.search(rf"(?<![a-z0-9]){re.escape(skill.casefold())}(?![a-z0-9])", description)
    ]
    candidate_normalized = {skill.casefold() for skill in candidate}
    matched = [skill for skill in required if skill.casefold() in candidate_normalized]
    gaps = [skill for skill in required if skill.casefold() not in candidate_normalized]
    return {
        "candidate_skills": candidate,
        "role_skills_found": required,
        "matched_skills": matched,
        "gaps": gaps,
        "unlisted_or_unrecognized_requirements": "Role descriptions can include skills outside the configured catalog.",
    }


def assign_risk(job: dict, score: int, config: dict) -> tuple[str, str]:
    thresholds = config["thresholds"]
    minimum = int(thresholds["minimum_match_score"])
    auto_minimum = int(thresholds["auto_submit_match_score"])
    constraints = config["constraints"]
    rules = config.get("risk_rules", DEFAULT_CONFIG["risk_rules"])

    if score < minimum:
        return "reject", "Match score is below the configured minimum."
    if (
        rules["reject_no_sponsorship_when_required"]
        and constraints.get("require_sponsorship") is True
        and job.get("sponsorship_status") == "no_sponsorship"
    ):
        return "reject", "Employer explicitly states that sponsorship is unavailable."
    if (
        rules["reject_unanswerable_custom_ats"]
        and job.get("application_flow_type") == "custom_ats"
        and not job.get("screening_questions_answerable")
    ):
        return "reject", "Custom ATS has required screening questions without configured answers."

    exact_constraints_match = all(job.get(key) is True for key in rules["require_exact_matches"])
    sponsorship_constraint_configured = (
        not rules["require_configured_sponsorship_constraint_for_auto_tier"]
        or constraints.get("require_sponsorship") is not None
    )
    if (
        (not rules["require_easy_apply_for_auto_tier"] or job.get("application_flow_type") == "easy_apply")
        and score >= auto_minimum
        and (
            not rules["require_answerable_screening_for_auto_tier"]
            or job.get("screening_questions_answerable") is True
        )
        and exact_constraints_match
        and sponsorship_constraint_configured
        and (
            constraints.get("require_sponsorship") is not True
            or job.get("sponsorship_status") == "evidence_found"
        )
    ):
        return "auto_submit", "Eligible by configured rules; recorded for dry-run only, not submitted."
    if constraints.get("require_sponsorship") is True and job.get("sponsorship_status") != "evidence_found":
        return "batch_review", "Sponsorship evidence is unknown; verify it before any application action."
    return "batch_review", "Requires human review before any application action."


def score_jobs(jobs: list[dict], config: dict, max_concurrency: int | None = None) -> list[dict]:
    """Score a batch with bounded local workers; no remote calls are made."""
    worker_count = max(1, int(max_concurrency or config["limits"]["max_concurrency"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(lambda job: score_job(job, config), jobs))


class JobStore:
    def __init__(self, path: str | Path = "data/jobs.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    description TEXT NOT NULL,
                    flow_type TEXT NOT NULL,
                    score INTEGER,
                    score_reason TEXT,
                    missing_keywords TEXT NOT NULL DEFAULT '[]',
                    tier TEXT NOT NULL DEFAULT 'unscored',
                    decision_reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    source TEXT NOT NULL DEFAULT 'manual_csv',
                    outcome TEXT NOT NULL DEFAULT 'unknown',
                    sponsorship_status TEXT NOT NULL DEFAULT 'unknown',
                    sponsorship_evidence TEXT NOT NULL DEFAULT '',
                    sponsorship_provenance TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS platform_state (
                    platform TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0,
                    consecutive_errors INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS materials (
                    job_id INTEGER PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    contents TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS search_preferences (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    preferences_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Existing local databases predate the sponsorship columns.
            existing = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            for name, definition in {
                "sponsorship_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "sponsorship_evidence": "TEXT NOT NULL DEFAULT ''",
                "sponsorship_provenance": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def _audit(self, connection: sqlite3.Connection, job_id: int | None, event: str, details: dict) -> None:
        connection.execute(
            "INSERT INTO audit_log(job_id, timestamp, event, details) VALUES (?, ?, ?, ?)",
            (job_id, _now(), event, json.dumps(details, ensure_ascii=False)),
        )

    def add_job(self, raw_job: dict, source: str = "manual_csv") -> tuple[int | None, bool]:
        job = normalize_job(raw_job)
        timestamp = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO jobs
                (url, title, company, location, description, flow_type, source, sponsorship_status,
                 sponsorship_evidence, sponsorship_provenance, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job["url"], job["title"], job["company"], job["location"], job["description"],
                    job["application_flow_type"], source, job["sponsorship_status"], job["sponsorship_evidence"],
                    job["sponsorship_provenance"], json.dumps(job, ensure_ascii=False), timestamp, timestamp,
                ),
            )
            if cursor.rowcount == 0:
                self._audit(connection, None, "deduplicated", {"url": job["url"], "source": source})
                return None, False
            job_id = cursor.lastrowid
            self._audit(connection, job_id, "ingested", {"url": job["url"], "source": source})
            return job_id, True

    def import_csv(self, file_obj, source: str = "manual_csv") -> dict:
        reader = csv.DictReader(file_obj)
        added = duplicates = invalid = 0
        errors = []
        for line_number, row in enumerate(reader, start=2):
            try:
                _, was_added = self.add_job(row, source)
                added += int(was_added)
                duplicates += int(not was_added)
            except (ValueError, sqlite3.Error) as error:
                invalid += 1
                errors.append(f"Row {line_number}: {error}")
        return {"added": added, "duplicates": duplicates, "invalid": invalid, "errors": errors}

    def score_unscored(self, config: dict) -> int:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE score IS NULL ORDER BY id").fetchall()
        jobs = [json.loads(row["payload"]) for row in rows]
        score_results = score_jobs(jobs, config, max_concurrency=config["limits"]["max_concurrency"])
        count = 0
        for row, result in zip(rows, score_results):
            job = json.loads(row["payload"])
            tier, decision = assign_risk(job, result["score"], config)
            status = "rejected" if tier == "reject" else "pending_review"
            with self._connect() as connection:
                connection.execute(
                    """UPDATE jobs SET score=?, score_reason=?, missing_keywords=?, tier=?,
                    decision_reason=?, status=?, updated_at=? WHERE id=?""",
                    (
                        result["score"], result["reason"], json.dumps(result["missing_keywords"]),
                        tier, decision, status, _now(), row["id"],
                    ),
                )
                self._audit(
                    connection,
                    row["id"],
                    "scored",
                    {**result, "tier": tier, "decision_reason": decision, "action": "dry_run_only"},
                )
            count += 1
        return count

    def list_jobs(self, status: str | None = None) -> list[dict]:
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    """SELECT * FROM jobs WHERE status=?
                    ORDER BY CASE sponsorship_status WHEN 'evidence_found' THEN 2 WHEN 'unknown' THEN 1 ELSE 0 END DESC,
                    score DESC, id DESC""", (status,)
                )
            else:
                rows = connection.execute(
                    """SELECT * FROM jobs
                    ORDER BY CASE sponsorship_status WHEN 'evidence_found' THEN 2 WHEN 'unknown' THEN 1 ELSE 0 END DESC,
                    score DESC, id DESC"""
                )
            return [dict(row) for row in rows.fetchall()]

    def load_search_preferences(self, config: dict) -> dict:
        defaults = default_search_preferences(config)
        with self._connect() as connection:
            row = connection.execute("SELECT preferences_json FROM search_preferences WHERE id=1").fetchone()
        if not row:
            return defaults
        saved = json.loads(row["preferences_json"])
        defaults.update(saved)
        return defaults

    def save_search_preferences(self, preferences: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO search_preferences(id, preferences_json, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET preferences_json=excluded.preferences_json, updated_at=excluded.updated_at""",
                (json.dumps(preferences, ensure_ascii=False), _now()),
            )

    def decide_review(self, job_ids: list[int], decision: str) -> int:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Review decision must be approved or rejected")
        changed = 0
        with self._connect() as connection:
            for job_id in job_ids:
                cursor = connection.execute(
                    "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status='pending_review'",
                    (decision, _now(), job_id),
                )
                if cursor.rowcount:
                    self._audit(connection, job_id, "batch_review_decision", {"decision": decision})
                    changed += 1
        return changed

    def record_outcome(self, job_id: int, outcome: str) -> None:
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Outcome must be one of {sorted(VALID_OUTCOMES)}")
        with self._connect() as connection:
            connection.execute("UPDATE jobs SET outcome=?, updated_at=? WHERE id=?", (outcome, _now(), job_id))
            self._audit(connection, job_id, "outcome_recorded", {"outcome": outcome})

    def can_auto_submit(self, config: dict) -> tuple[bool, str]:
        if not config.get("auto_apply_enabled", False):
            return False, "Auto-apply kill switch is disabled."
        today = date.today().isoformat()
        with self._connect() as connection:
            count = connection.execute(
                """SELECT COUNT(*) FROM audit_log
                WHERE event='application_submitted' AND substr(timestamp, 1, 10)=?""",
                (today,),
            ).fetchone()[0]
        if count >= int(config["limits"]["daily_volume"]):
            return False, "Configured daily application volume cap reached."
        return False, "Submission workers are intentionally not implemented; use the review queue."

    def get_or_create_materials(self, job_id: int, generator) -> dict:
        """Cache per-job material output; the generator must use verified candidate facts."""
        with self._connect() as connection:
            row = connection.execute("SELECT contents FROM materials WHERE job_id=?", (job_id,)).fetchone()
            if row:
                return json.loads(row["contents"])
            job_row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job_row:
                raise ValueError(f"Unknown job id: {job_id}")
            material = generator(dict(job_row))
            contents = json.dumps(material, ensure_ascii=False)
            connection.execute(
                "INSERT INTO materials(job_id, generated_at, contents) VALUES (?, ?, ?)",
                (job_id, _now(), contents),
            )
            self._audit(connection, job_id, "materials_cached", {"keys": sorted(material)})
            return material

    def record_platform_result(self, platform: str, success: bool, config: dict, reason: str = "") -> bool:
        """Record errors and pause a platform after repeated failures; never bypass blocks."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM platform_state WHERE platform=?", (platform,)).fetchone()
            errors = 0 if success else (row["consecutive_errors"] if row else 0) + 1
            paused = bool(row["paused"]) if row else False
            threshold = int(config["safety"]["pause_after_platform_errors"])
            if not success and errors >= threshold:
                paused = True
                reason = reason or f"Paused after {errors} consecutive platform errors."
            if success:
                reason = ""
            connection.execute(
                """INSERT INTO platform_state(platform, paused, consecutive_errors, reason, updated_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(platform) DO UPDATE SET
                paused=excluded.paused, consecutive_errors=excluded.consecutive_errors,
                reason=excluded.reason, updated_at=excluded.updated_at""",
                (platform, int(paused), errors, reason, _now()),
            )
            return paused

    def audit_rows(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT audit_log.*, jobs.url, jobs.title, jobs.company
                FROM audit_log LEFT JOIN jobs ON jobs.id=audit_log.job_id
                ORDER BY audit_log.id DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def platform_rows(self) -> list[dict]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM platform_state ORDER BY platform")]


def accuracy_report(jobs: list[dict]) -> dict:
    groups = {"auto_submit": [], "batch_review_approved": []}
    for job in jobs:
        if job.get("tier") == "auto_submit":
            groups["auto_submit"].append(job)
        elif job.get("status") == "approved":
            groups["batch_review_approved"].append(job)

    report = {}
    for group, entries in groups.items():
        known = [entry for entry in entries if entry.get("outcome") in VALID_OUTCOMES - {"unknown"}]
        responses = sum(entry["outcome"] in RESPONSE_OUTCOMES for entry in known)
        report[group] = {
            "total": len(entries),
            "outcomes_recorded": len(known),
            "responses": responses,
            "response_rate_percent": round(100 * responses / len(known), 1) if known else None,
        }
    return report


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
