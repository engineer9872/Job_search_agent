import unittest
from DB import SessionLocal
from api.routes.jobs import get_jobs


class TestE2EPipeline(unittest.TestCase):
    """
    Part F.4 — End-to-end multi-platform fixture filtering pipeline assertion.
    """

    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_e2e_filtered_jobs_pipeline(self):
        res = get_jobs(
            title="Software Engineer",
            remote_only=True,
            job_type="contractor",
            limit=20,
            offset=0,
            db=self.db,
        )
        self.assertIn("jobs", res)
        self.assertIn("filter_hash", res)
        self.assertEqual(len(res["filter_hash"]), 64)

        for job in res["jobs"]:
            self.assertTrue(job["remote_flag"])
            self.assertEqual(job["job_type"], "contract")


if __name__ == "__main__":
    unittest.main()
