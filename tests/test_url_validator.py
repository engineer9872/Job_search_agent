import unittest
from pipeline.filter_guard import validate_direct_job_url, PLATFORM_URL_PATTERNS


class TestUrlValidatorGuard(unittest.TestCase):
    """
    Unit test suite for the Direct Job-Posting URL Validation Guard.
    Asserts per-platform regex pattern checks accept valid direct posting URLs
    and reject generic search, aggregator, or Google search URLs.
    """

    def test_all_10_platforms_configured(self):
        expected_platforms = {
            "linkedin", "indeed", "glassdoor", "dice", "ziprecruiter",
            "usajobs", "careerbuilder", "simplyhired", "weworkremotely", "hired"
        }
        self.assertEqual(set(PLATFORM_URL_PATTERNS.keys()), expected_platforms)

    def test_linkedin_url_validation(self):
        passing = "https://www.linkedin.com/jobs/view/3829104821/"
        failing = "https://www.google.com/search?q=linkedin+developer+jobs"
        failing_search = "https://www.linkedin.com/jobs/search/?keywords=developer"

        self.assertTrue(validate_direct_job_url("linkedin", passing))
        self.assertFalse(validate_direct_job_url("linkedin", failing))
        self.assertFalse(validate_direct_job_url("linkedin", failing_search))

    def test_indeed_url_validation(self):
        passing = "https://www.indeed.com/viewjob?jk=7c8d9e0f1a2b3c4d"
        failing = "https://www.indeed.com/jobs?q=developer&l=Remote"

        self.assertTrue(validate_direct_job_url("indeed", passing))
        self.assertFalse(validate_direct_job_url("indeed", failing))

    def test_glassdoor_url_validation(self):
        passing = "https://www.glassdoor.com/job-listing/senior-python-developer-company-JV_IC1147401_KO0,23_KE24,31.htm?jl=10089201"
        failing = "https://www.glassdoor.com/Job/jobs.htm?suggestCount=0&suggestGiven=false"

        self.assertTrue(validate_direct_job_url("glassdoor", passing))
        self.assertFalse(validate_direct_job_url("glassdoor", failing))

    def test_dice_url_validation(self):
        passing = "https://www.dice.com/job-detail/38a910bf-7c82-419b-a18c-30910f182bc9"
        failing = "https://www.dice.com/jobs?q=developer&location=Remote"

        self.assertTrue(validate_direct_job_url("dice", passing))
        self.assertFalse(validate_direct_job_url("dice", failing))

    def test_ziprecruiter_url_validation(self):
        passing = "https://www.ziprecruiter.com/jobs/acme-corp-1234/senior-developer-5678"
        failing = "https://www.ziprecruiter.com/candidate/search?search=developer"

        self.assertTrue(validate_direct_job_url("ziprecruiter", passing))
        self.assertFalse(validate_direct_job_url("ziprecruiter", failing))

    def test_usajobs_url_validation(self):
        passing = "https://www.usajobs.gov/job/789101100"
        failing = "https://www.usajobs.gov/Search/Results?k=developer"

        self.assertTrue(validate_direct_job_url("usajobs", passing))
        self.assertFalse(validate_direct_job_url("usajobs", failing))

    def test_careerbuilder_url_validation(self):
        passing = "https://www.careerbuilder.com/job/J3N58R67T80B3Y9W0L0"
        failing = "https://www.careerbuilder.com/jobs?keywords=python"

        self.assertTrue(validate_direct_job_url("careerbuilder", passing))
        self.assertFalse(validate_direct_job_url("careerbuilder", failing))

    def test_simplyhired_url_validation(self):
        passing = "https://www.simplyhired.com/job/A8B9C0D1E2F3"
        failing = "https://www.simplyhired.com/search?q=developer"

        self.assertTrue(validate_direct_job_url("simplyhired", passing))
        self.assertFalse(validate_direct_job_url("simplyhired", failing))

    def test_weworkremotely_url_validation(self):
        passing = "https://weworkremotely.com/remote-jobs/acme-inc-senior-backend-engineer"
        failing = "https://weworkremotely.com/categories/remote-full-stack-programming-jobs"

        self.assertTrue(validate_direct_job_url("weworkremotely", passing))
        self.assertFalse(validate_direct_job_url("weworkremotely", failing))

    def test_hired_url_validation(self):
        passing = "https://hired.com/jobs/12345-senior-python-engineer"
        failing = "https://hired.com/employers"

        self.assertTrue(validate_direct_job_url("hired", passing))
        self.assertFalse(validate_direct_job_url("hired", failing))


if __name__ == "__main__":
    unittest.main()
