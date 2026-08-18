import os
import unittest
from pipeline.normalize import NormalizedJob
from pipeline.enrich import RecruiterEnricher, extract_emails, extract_recruiter_name_near_email, is_generic_email
from Scheduler.crons_jobs import RateQuotaTracker


class TestEnrichment(unittest.TestCase):

    def test_generic_email_detection(self):
        self.assertTrue(is_generic_email("careers@company.com"))
        self.assertTrue(is_generic_email("jobs@acme.org"))
        self.assertTrue(is_generic_email("privacy@startup.io"))
        self.assertFalse(is_generic_email("alex.vance@company.com"))
        self.assertFalse(is_generic_email("jane.doe@tech.net"))

    def test_email_separation(self):
        text = "For job inquiries contact careers@acme.com or reach out to recruiter alex.vance@acme.com directly."
        recruiter_email, company_contact_email = extract_emails(text)
        self.assertEqual(company_contact_email, "careers@acme.com")
        self.assertEqual(recruiter_email, "alex.vance@acme.com")

    def test_recruiter_name_context_window(self):
        # Case A: Recruiter email present with name near it -> Should extract name
        text_with_email = "Recruiter: Sarah Connor (sarah.connor@cyberdyne.com). Please send resume."
        name = extract_recruiter_name_near_email(text_with_email, "sarah.connor@cyberdyne.com")
        self.assertEqual(name, "Sarah Connor")

        # Case B: Standalone name without recruiter email -> Should return None
        text_no_email = "Recruiter Sarah Connor will review applications. Send CV to careers@cyberdyne.com."
        recruiter_email, company_contact = extract_emails(text_no_email)
        self.assertIsNone(recruiter_email)  # careers@ is generic
        name_without_email = extract_recruiter_name_near_email(text_no_email, recruiter_email)
        self.assertIsNone(name_without_email)

    def test_enricher_batch(self):
        job = NormalizedJob(
            title="Senior Engineer",
            company="Acme Corp",
            source_platform="greenhouse",
            apply_url="https://example.com/apply/1",
            description_snippet="Contact Hiring Manager: John Smith (john.smith@acme.com) or careers@acme.com",
        )
        enricher = RecruiterEnricher()
        enricher.enrich_job(job)

        self.assertEqual(job.recruiter_email, "john.smith@acme.com")
        self.assertEqual(job.company_contact_email, "careers@acme.com")
        self.assertEqual(job.recruiter_name, "John Smith")


class TestQuotaTracker(unittest.TestCase):

    def test_quota_limits(self):
        test_file = "d:/Job_search_agent/Scheduler/test_quota_state.json"
        if os.path.exists(test_file):
            os.remove(test_file)

        tracker = RateQuotaTracker(quotas={"test_src": 2}, state_file=test_file)
        self.assertTrue(tracker.can_fetch("test_src"))
        tracker.record_call("test_src", 1)
        self.assertTrue(tracker.can_fetch("test_src"))
        tracker.record_call("test_src", 1)

        # 3rd call exceeds quota limit of 2 -> should return False
        self.assertFalse(tracker.can_fetch("test_src"))

        # Verify state persistence across restart
        tracker2 = RateQuotaTracker(quotas={"test_src": 2}, state_file=test_file)
        self.assertFalse(tracker2.can_fetch("test_src"))

        if os.path.exists(test_file):
            os.remove(test_file)


if __name__ == "__main__":
    unittest.main()
