import io
import tempfile
import unittest
from pathlib import Path

from job_pipeline import (
    DEFAULT_CONFIG,
    JobStore,
    apply_search_preferences,
    accuracy_report,
    assign_risk,
    analyze_skill_gap,
    canonicalize_url,
    normalize_job,
    score_job,
)


def eligible_job(**overrides):
    job = {
        "url": "https://example.com/jobs/data-analyst",
        "title": "Senior Data Analyst",
        "company": "Example Ltd",
        "location": "United Kingdom",
        "description": " ".join(DEFAULT_CONFIG["profile"]["skills"]),
        "application_flow_type": "easy_apply",
        "screening_questions_answerable": True,
        "location_match": True,
        "seniority_match": True,
        "visa_match": True,
    }
    job.update(overrides)
    return job


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp_dir.name) / "jobs.sqlite3")
        self.config = {key: value.copy() if isinstance(value, dict) else value for key, value in DEFAULT_CONFIG.items()}
        self.config["thresholds"] = DEFAULT_CONFIG["thresholds"].copy()
        self.config["limits"] = DEFAULT_CONFIG["limits"].copy()
        self.config["constraints"] = DEFAULT_CONFIG["constraints"].copy()
        self.config["constraints"]["require_sponsorship"] = False
        self.config["profile"] = {
            **DEFAULT_CONFIG["profile"],
            "target_titles": list(DEFAULT_CONFIG["profile"]["target_titles"]),
            "skills": list(DEFAULT_CONFIG["profile"]["skills"]),
            "screening_answers": DEFAULT_CONFIG["profile"]["screening_answers"].copy(),
        }
        self.config["safety"] = DEFAULT_CONFIG["safety"].copy()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_score_has_structured_explanation(self):
        result = score_job(normalize_job(eligible_job()), self.config)
        self.assertEqual(result["score"], 100)
        self.assertIn("Matched", result["reason"])
        self.assertEqual(result["missing_keywords"], [])
        self.assertEqual(result["method"], "offline_keyword_overlap")

    def test_skill_gap_separates_resume_skills_from_role_skills(self):
        gap = analyze_skill_gap("We need SQL, Python, Power BI, Snowflake and dbt.", self.config["profile"])
        self.assertIn("SQL", gap["matched_skills"])
        self.assertIn("Python", gap["matched_skills"])
        self.assertIn("Snowflake", gap["gaps"])
        self.assertIn("dbt", gap["gaps"])

    def test_risk_tiers_cover_auto_review_and_reject(self):
        job = normalize_job(eligible_job())
        self.assertEqual(assign_risk(job, 90, self.config)[0], "auto_submit")
        self.assertEqual(assign_risk({**job, "location_match": False}, 90, self.config)[0], "batch_review")
        self.assertEqual(assign_risk(job, 74, self.config)[0], "reject")
        custom = {**job, "application_flow_type": "custom_ats", "screening_questions_answerable": False}
        self.assertEqual(assign_risk(custom, 90, self.config)[0], "reject")

    def test_unknown_sponsorship_preference_blocks_auto_tier(self):
        self.config["constraints"]["require_sponsorship"] = None
        job = normalize_job(eligible_job())
        self.assertEqual(assign_risk(job, 90, self.config)[0], "batch_review")

    def test_sponsorship_constraint_rejects_explicit_no(self):
        self.config["constraints"]["require_sponsorship"] = True
        job = normalize_job(eligible_job(explicit_no_sponsorship=True))
        tier, reason = assign_risk(job, 90, self.config)
        self.assertEqual(tier, "reject")
        self.assertIn("sponsorship", reason)

    def test_unknown_sponsorship_evidence_never_reaches_auto_tier_when_required(self):
        self.config["constraints"]["require_sponsorship"] = True
        job = normalize_job(eligible_job(sponsorship_status="unknown"))
        tier, reason = assign_risk(job, 90, self.config)
        self.assertEqual(tier, "batch_review")
        self.assertIn("evidence", reason)

    def test_search_preferences_persist_and_affect_runtime_scoring_config(self):
        preferences = {
            "redundancy_target_date": "2026-12-31",
            "work_locations": ["London", "Hybrid"],
            "salary_minimum": "65000",
            "salary_maximum": "85000",
            "require_sponsorship": True,
            "visa_constraint_note": "Requires sponsorship",
            "target_titles": ["Analytics Lead"],
        }
        self.store.save_search_preferences(preferences)
        self.assertEqual(self.store.load_search_preferences(self.config), preferences)
        effective = apply_search_preferences(self.config, preferences)
        self.assertEqual(effective["constraints"]["location"], "London, Hybrid")
        self.assertTrue(effective["constraints"]["require_sponsorship"])
        self.assertEqual(effective["profile"]["target_titles"], ["Analytics Lead"])

    def test_url_dedupe_and_local_import(self):
        canonical = canonicalize_url("https://www.example.com/job/1/?utm_source=feed&jobId=2#apply")
        self.assertEqual(canonical, "https://example.com/job/1?jobId=2")
        first, added = self.store.add_job(eligible_job(url="https://example.com/job/1?utm_source=feed"))
        duplicate, was_added = self.store.add_job(eligible_job(url="https://www.example.com/job/1"))
        self.assertTrue(added)
        self.assertFalse(was_added)
        self.assertIsNone(duplicate)
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_csv_rows_are_validated_and_deduplicated(self):
        csv_text = "url,title,company,location,description,application_flow_type\nhttps://example.com/a,Analyst,Acme,UK,SQL,unknown\nhttps://example.com/a,Analyst,Acme,UK,SQL,unknown\n"
        result = self.store.import_csv(io.StringIO(csv_text))
        self.assertEqual((result["added"], result["duplicates"], result["invalid"]), (1, 1, 0))

    def test_review_actions_and_audit_are_recorded(self):
        job_id, _ = self.store.add_job(eligible_job(location_match=False))
        self.store.score_unscored(self.config)
        self.assertEqual(self.store.decide_review([job_id], "approved"), 1)
        self.assertEqual(self.store.list_jobs()[0]["status"], "approved")
        events = [row["event"] for row in self.store.audit_rows()]
        self.assertIn("ingested", events)
        self.assertIn("scored", events)
        self.assertIn("batch_review_decision", events)

    def test_materials_are_generated_once_per_job(self):
        job_id, _ = self.store.add_job(eligible_job())
        calls = []
        generator = lambda job: calls.append(job["title"]) or {"resume_variant": "draft-1"}
        self.assertEqual(self.store.get_or_create_materials(job_id, generator)["resume_variant"], "draft-1")
        self.assertEqual(self.store.get_or_create_materials(job_id, generator)["resume_variant"], "draft-1")
        self.assertEqual(calls, ["Senior Data Analyst"])

    def test_kill_switch_and_cap_never_enable_submission_worker(self):
        allowed, reason = self.store.can_auto_submit(self.config)
        self.assertFalse(allowed)
        self.assertIn("kill switch", reason)
        enabled_config = {**self.config, "auto_apply_enabled": True}
        allowed, reason = self.store.can_auto_submit(enabled_config)
        self.assertFalse(allowed)
        self.assertIn("not implemented", reason)

    def test_platform_pauses_after_configured_consecutive_errors(self):
        self.config["safety"]["pause_after_platform_errors"] = 2
        self.assertFalse(self.store.record_platform_result("example", False, self.config, "error"))
        self.assertTrue(self.store.record_platform_result("example", False, self.config, "blocked"))
        self.assertTrue(self.store.platform_rows()[0]["paused"])

    def test_accuracy_compares_response_rates(self):
        jobs = [
            {"tier": "auto_submit", "status": "pending_review", "outcome": "interview"},
            {"tier": "auto_submit", "status": "pending_review", "outcome": "ghosted"},
            {"tier": "batch_review", "status": "approved", "outcome": "rejected"},
            {"tier": "batch_review", "status": "approved", "outcome": "unknown"},
        ]
        result = accuracy_report(jobs)
        self.assertEqual(result["auto_submit"]["response_rate_percent"], 50.0)
        self.assertEqual(result["batch_review_approved"]["response_rate_percent"], 100.0)


if __name__ == "__main__":
    unittest.main()
