"""Persistent application ledger and reusable reviewed answer library."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


_SUBMISSION_FINAL_STATUSES = (
    "submitted", "confirmation_received", "interview", "rejected", "offer", "withdrawn",
)


class ApplicationTracker:
    def __init__(self, database_path: str | Path = "data/jobs.sqlite3"):
        self.path = Path(database_path)
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

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY,
                    job_url TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    submitted_at TEXT,
                    form_snapshot TEXT NOT NULL DEFAULT '[]',
                    candidate_skills TEXT NOT NULL DEFAULT '[]',
                    required_skills TEXT NOT NULL DEFAULT '[]',
                    skill_gaps TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS answer_library (
                    question_key TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    reviewed_by_user INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_resume (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    file_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    file_data BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_events (
                    id INTEGER PRIMARY KEY,
                    application_id INTEGER,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(application_id) REFERENCES applications(id)
                );
                CREATE TABLE IF NOT EXISTS submission_claims (
                    job_url TEXT PRIMARY KEY,
                    application_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def save_form_plan(
        self,
        job_url: str,
        role: str,
        company: str,
        fields: list[dict],
        status: str = "in_progress",
        candidate_skills: list[str] | None = None,
        required_skills: list[str] | None = None,
        skill_gaps: list[str] | None = None,
    ) -> int:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO applications
                (job_url, role, company, status, created_at, updated_at, form_snapshot,
                 candidate_skills, required_skills, skill_gaps)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    role=excluded.role, company=excluded.company, updated_at=excluded.updated_at,
                    form_snapshot=excluded.form_snapshot, candidate_skills=excluded.candidate_skills,
                    required_skills=excluded.required_skills, skill_gaps=excluded.skill_gaps,
                    status=CASE WHEN applications.submitted_at IS NOT NULL
                        OR applications.status IN ('submission_attempted', 'submitted', 'confirmation_received',
                                                   'interview', 'rejected', 'offer', 'withdrawn')
                        THEN applications.status ELSE excluded.status END""",
                (
                    job_url, role, company, status, now, now, json.dumps(fields, ensure_ascii=False),
                    json.dumps(candidate_skills or [], ensure_ascii=False),
                    json.dumps(required_skills or [], ensure_ascii=False),
                    json.dumps(skill_gaps or [], ensure_ascii=False),
                ),
            )
            row = connection.execute("SELECT id FROM applications WHERE job_url=?", (job_url,)).fetchone()
            return int(row["id"])

    def mark_submitted(self, application_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE applications SET status='submitted', submitted_at=?, updated_at=? WHERE id=?",
                (_now(), _now(), application_id),
            )

    def claim_submission(self, job_url: str, application_id: int) -> bool:
        """Reserve one attempt atomically, including ledgers from older versions.

        A final status or submitted timestamp predates the claims table on some
        installations. Those records are equally authoritative. An existing
        unconfirmed attempt also needs a positive browser refusal before retry.
        """
        final_statuses = (*_SUBMISSION_FINAL_STATUSES, "submission_attempted")
        placeholders = ",".join("?" for _ in final_statuses)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""INSERT OR IGNORE INTO submission_claims(job_url, application_id, created_at)
                    SELECT ?, ?, ? WHERE EXISTS (
                        SELECT 1 FROM applications WHERE id=? AND job_url=?
                        AND submitted_at IS NULL AND status NOT IN ({placeholders})
                    )""",
                (job_url, application_id, _now(), application_id, job_url, *final_statuses),
            )
            return cursor.rowcount == 1

    def release_submission(self, job_url: str) -> None:
        """Release only an attempt the browser positively reports it did not click."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                """SELECT applications.id, applications.status, applications.submitted_at
                   FROM submission_claims JOIN applications
                     ON applications.id=submission_claims.application_id
                   WHERE submission_claims.job_url=?""", (job_url,),
            ).fetchone()
            if not record or record["submitted_at"] or record["status"] in _SUBMISSION_FINAL_STATUSES:
                return
            connection.execute("DELETE FROM submission_claims WHERE job_url=?", (job_url,))
            connection.execute(
                """UPDATE applications SET status='ready_to_submit', updated_at=?
                   WHERE id=? AND status='submission_attempted'""", (_now(), record["id"]),
            )

    def submission_record(self, job_url: str) -> dict | None:
        """Return submission metadata for restoring a run without exposing answers."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT applications.id AS application_id, applications.status,
                          submission_claims.application_id IS NOT NULL AS claimed,
                          COALESCE(submission_claims.created_at, applications.created_at) AS created_at
                   FROM applications LEFT JOIN submission_claims
                     ON submission_claims.job_url=applications.job_url
                    AND submission_claims.application_id=applications.id
                   WHERE applications.job_url=?""", (job_url,),
            ).fetchone()
        if not row:
            return None
        return {"application_id": int(row["application_id"]), "status": row["status"],
                "claimed": bool(row["claimed"]), "created_at": row["created_at"]}

    def submissions_today(self) -> int:
        """Count local, confirmation-backed submissions for the daily policy cap."""
        # submitted_at is stored in UTC by _now(), so calculate the cap day in
        # UTC too. Mixing a local calendar date with UTC timestamps made the
        # cap disappear for several hours around midnight.
        utc_today = datetime.now(UTC).date().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM applications WHERE substr(submitted_at, 1, 10)=?",
                (utc_today,),
            ).fetchone()
        return int(row["count"])

    def update_status(self, application_id: int, status: str, notes: str = "") -> None:
        allowed = {
            "in_progress", "waiting_for_login", "needs_user_input", "ready_to_submit",
            "submission_attempted", "submitted", "confirmation_received", "interview",
            "rejected", "offer", "withdrawn", "automation_failed",
        }
        if status not in allowed:
            raise ValueError(f"Status must be one of {sorted(allowed)}")
        with self._connect() as connection:
            submitted_at = _now() if status == "submitted" else None
            connection.execute(
                "UPDATE applications SET status=?, submitted_at=COALESCE(?, submitted_at), notes=?, updated_at=? WHERE id=?",
                (status, submitted_at, notes, _now(), application_id),
            )

    def record_automation_event(self, application_id: int | None, event: str, details: dict | None = None) -> None:
        """Keep a local, inspectable trail of every browser-agent decision."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO automation_events(application_id, timestamp, event, details) VALUES (?, ?, ?, ?)",
                (application_id, _now(), event, json.dumps(details or {}, ensure_ascii=False)),
            )

    def automation_events(self, application_id: int | None = None) -> list[dict]:
        with self._connect() as connection:
            if application_id is None:
                rows = connection.execute("SELECT * FROM automation_events ORDER BY id DESC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM automation_events WHERE application_id=? ORDER BY id DESC", (application_id,)
                ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def list_applications(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM applications ORDER BY updated_at DESC, id DESC").fetchall()
        records = []
        for row in rows:
            record = dict(row)
            for key in ["form_snapshot", "candidate_skills", "required_skills", "skill_gaps"]:
                record[key] = json.loads(record[key])
            records.append(record)
        return records

    def save_answer(self, question_key: str, prompt: str, response: str, reviewed: bool = True) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO answer_library(question_key, prompt, response, reviewed_by_user, updated_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(question_key) DO UPDATE SET
                prompt=excluded.prompt, response=excluded.response,
                reviewed_by_user=excluded.reviewed_by_user, updated_at=excluded.updated_at""",
                (question_key, prompt, response, int(reviewed), _now()),
            )

    def answer_library(self) -> dict[str, dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM answer_library ORDER BY question_key").fetchall()
        return {row["question_key"]: dict(row) for row in rows}

    def save_profile(self, profile: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO candidate_profile(id, profile_json, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
                (json.dumps(profile, ensure_ascii=False), _now()),
            )

    def load_profile(self) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT profile_json FROM candidate_profile WHERE id=1").fetchone()
        return json.loads(row["profile_json"]) if row else {}

    def save_resume(self, file_name: str, mime_type: str, file_data: bytes) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO candidate_resume(id, file_name, mime_type, file_data, updated_at)
                VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                file_name=excluded.file_name, mime_type=excluded.mime_type,
                file_data=excluded.file_data, updated_at=excluded.updated_at""",
                (file_name, mime_type, sqlite3.Binary(file_data), _now()),
            )

    def load_resume(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT file_name, mime_type, file_data FROM candidate_resume WHERE id=1").fetchone()
        if not row:
            return None
        return {"name": row["file_name"], "mime_type": row["mime_type"], "data": bytes(row["file_data"])}

    def delete_resume(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM candidate_resume WHERE id=1")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
