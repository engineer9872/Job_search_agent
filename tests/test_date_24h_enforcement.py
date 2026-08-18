import unittest
from datetime import datetime, timezone, timedelta

from pipeline.filter_lock import FilterSpec, create_locked_filter_spec
from pipeline.filter_guard import ThreeTierFilterGuard, validate_direct_job_url


class TestDate24hEnforcement(unittest.TestCase):
    """
    End-to-end integration test for strict 'Past 24 Hours' date enforcement
    and direct URL validation guard.
    """

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.twenty_hours_ago = (self.now - timedelta(hours=20)).isoformat()
        self.thirty_six_hours_ago = (self.now - timedelta(hours=36)).isoformat()
        self.five_days_ago = (self.now - timedelta(days=5)).isoformat()

        self.candidates = [
            {
                "id": "job_24h_valid_1",
                "title": "Senior Python Engineer",
                "company": "Tech Corp",
                "platform_id": "linkedin",
                "url": "https://www.linkedin.com/jobs/view/123456789/",
                "country": "US",
                "remote_flag": True,
                "job_type": "full_time",
                "posted_date": self.twenty_hours_ago,
            },
            {
                "id": "job_24h_valid_2",
                "title": "Backend Developer",
                "company": "Data Inc",
                "platform_id": "indeed",
                "url": "https://www.indeed.com/viewjob?jk=abcdef123456",
                "country": "US",
                "remote_flag": True,
                "job_type": "full_time",
                "posted_date": self.twenty_hours_ago,
            },
            {
                "id": "job_old_36h",
                "title": "React Engineer",
                "company": "Web Inc",
                "platform_id": "dice",
                "url": "https://www.dice.com/job-detail/998877",
                "country": "US",
                "remote_flag": True,
                "job_type": "full_time",
                "posted_date": self.thirty_six_hours_ago,
            },
            {
                "id": "job_old_5d",
                "title": "Full Stack Developer",
                "company": "Legacy Corp",
                "platform_id": "glassdoor",
                "url": "https://www.glassdoor.com/job-listing/dev-123.htm",
                "country": "US",
                "remote_flag": True,
                "job_type": "full_time",
                "posted_date": self.five_days_ago,
            },
            {
                "id": "job_24h_invalid_url",
                "title": "Python Specialist",
                "company": "Search Corp",
                "platform_id": "ziprecruiter",
                "url": "https://www.google.com/search?q=ziprecruiter+jobs",  # Invalid URL
                "country": "US",
                "remote_flag": True,
                "job_type": "full_time",
                "posted_date": self.twenty_hours_ago,
            },
            {
                "id": "job_24h_missing_date",
                "title": "Software Developer",
                "company": "NoDate Corp",
                "platform_id": "weworkremotely",
                "url": "https://weworkremotely.com/remote-jobs/nodate-dev",
                "country": "US",
                "remote_flag": True,
                "job_type": "full_time",
                "posted_date": None,  # Exclude when date filter is active
            },
        ]

    def test_past_24h_filter_enforcement(self):
        filter_spec = create_locked_filter_spec(
            job_title="all",
            country="US",
            remote_only=False,
            date_posted="past_24h",
            job_type="all",
            platform="all",
        )

        guard = ThreeTierFilterGuard(filter_spec)
        verified_jobs = guard.process_guard_checks(self.candidates)

        # Must retain strictly jobs within 24h that pass URL validation
        self.assertEqual(len(verified_jobs), 2)
        verified_ids = {j["id"] for j in verified_jobs}
        self.assertEqual(verified_ids, {"job_24h_valid_1", "job_24h_valid_2"})

        # Assert all verified jobs pass platform URL validation
        for job in verified_jobs:
            self.assertTrue(validate_direct_job_url(job["platform_id"], job["url"]))


if __name__ == "__main__":
    unittest.main()
