from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from application_tracking import ApplicationTracker


class SubmissionClaimTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "jobs.sqlite3"
        self.tracker = ApplicationTracker(self.database)
        self.url = "https://jobs.example.test/analyst/apply"
        self.application_id = self.tracker.save_form_plan(self.url, "Analyst", "Example", [])

    def test_claim_survives_restart_and_only_one_parallel_tab_wins(self):
        trackers = [ApplicationTracker(self.database) for _ in range(4)]
        barrier = threading.Barrier(len(trackers))

        def claim(tracker):
            barrier.wait()
            return tracker.claim_submission(self.url, self.application_id)

        with ThreadPoolExecutor(max_workers=len(trackers)) as executor:
            results = list(executor.map(claim, trackers))
        self.assertEqual(sum(results), 1)
        self.assertFalse(ApplicationTracker(self.database).claim_submission(self.url, self.application_id))

    def test_legacy_confirmed_statuses_cannot_be_claimed_without_claim_row(self):
        for status in ("submitted", "confirmation_received", "interview", "rejected", "offer", "withdrawn", "submission_attempted"):
            with self.subTest(status=status):
                url = self.url + "?status=" + status
                app_id = self.tracker.save_form_plan(url, "Analyst", "Example", [], status=status)
                self.assertFalse(self.tracker.claim_submission(url, app_id))

    def test_submitted_timestamp_blocks_claim_even_if_status_was_changed(self):
        self.tracker.mark_submitted(self.application_id)
        self.tracker.update_status(self.application_id, "in_progress")
        self.assertFalse(self.tracker.claim_submission(self.url, self.application_id))

    def test_application_id_must_belong_to_the_requested_url(self):
        self.assertFalse(self.tracker.claim_submission(self.url + "?different=1", self.application_id))
        self.assertFalse(self.tracker.claim_submission(self.url, self.application_id + 100))

    def test_replanning_does_not_hide_a_previous_attempt_or_later_outcome(self):
        for status in ("submission_attempted", "submitted", "confirmation_received", "interview", "rejected", "offer", "withdrawn"):
            with self.subTest(status=status):
                self.tracker.update_status(self.application_id, status)
                self.tracker.save_form_plan(self.url, "Analyst", "Example", [{"field": "Updated"}])
                self.assertEqual(self.tracker.submission_record(self.url)["status"], status)

    def test_positive_refusal_releases_claim_and_allows_explicit_retry(self):
        self.assertTrue(self.tracker.claim_submission(self.url, self.application_id))
        self.tracker.update_status(self.application_id, "submission_attempted")
        self.tracker.release_submission(self.url)
        record = self.tracker.submission_record(self.url)
        self.assertFalse(record["claimed"])
        self.assertEqual(record["status"], "ready_to_submit")
        self.assertTrue(self.tracker.claim_submission(self.url, self.application_id))

    def test_late_failure_cannot_release_confirmed_submission(self):
        self.assertTrue(self.tracker.claim_submission(self.url, self.application_id))
        self.tracker.mark_submitted(self.application_id)
        self.tracker.release_submission(self.url)
        record = self.tracker.submission_record(self.url)
        self.assertTrue(record["claimed"])
        self.assertEqual(record["status"], "submitted")
        self.assertFalse(self.tracker.claim_submission(self.url, self.application_id))

    def test_restore_record_contains_only_metadata_and_unknown_url_returns_none(self):
        self.assertIsNone(self.tracker.submission_record(self.url + "?unknown=1"))
        self.tracker.claim_submission(self.url, self.application_id)
        self.tracker.update_status(self.application_id, "submission_attempted")
        record = ApplicationTracker(self.database).submission_record(self.url)
        self.assertEqual(set(record), {"application_id", "status", "claimed", "created_at"})
        self.assertEqual(record["application_id"], self.application_id)
        self.assertTrue(record["claimed"])
        self.assertTrue(record["created_at"])


if __name__ == "__main__":
    unittest.main()
