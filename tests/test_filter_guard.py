import unittest
from pipeline.filter_lock import create_locked_filter_spec
from pipeline.filter_guard import ThreeTierFilterGuard


class TestThreeTierFilterGuard(unittest.TestCase):
    """
    Part F.3 — Fixture-based tests for 3-tier guard:
    - Check 1 structural match
    - Check 2 semantic disagreement flagging
    - Check 3 multi-tier corroboration arbitration
    """

    def setUp(self):
        self.spec = create_locked_filter_spec(
            job_title="Software Engineer",
            remote_only=True,
            job_type="contract",
            country="US",
        )
        self.guard = ThreeTierFilterGuard(self.spec)

    def test_check_1_pass_outright(self):
        job = {
            "id": "job_1",
            "title": "Software Engineer",
            "canonical_title": "Software Engineer",
            "remote_flag": True,
            "job_type": "contract",
            "country": "US",
            "platform_id": "linkedin",
            "url": "https://www.linkedin.com/jobs/view/123456789/",
            "description": "Awesome contract role for software engineer.",
        }
        self.assertTrue(self.guard.check_1_exact_structural_match(job))

    def test_check_1_fail_mismatch(self):
        job = {
            "id": "job_2",
            "title": "Software Engineer",
            "canonical_title": "Software Engineer",
            "remote_flag": False,  # Mismatch: spec requires remote_only=True
            "job_type": "contract",
            "country": "US",
            "platform_id": "linkedin",
            "url": "https://www.linkedin.com/jobs/view/123456789/",
        }
        self.assertFalse(self.guard.check_1_exact_structural_match(job))

    def test_check_2_disagreement_flagging(self):
        job = {
            "id": "job_3",
            "title": "Software Engineer",
            "remote_flag": True,
            "job_type": "contract",
            "country": "US",
            "description": "Must work in office, onsite only position.",  # Contradicts remote_flag
        }
        has_agreement, reason = self.guard.check_2_semantic_cross_validation(job)
        self.assertFalse(has_agreement)
        self.assertIsNotNone(reason)

    def test_check_3_arbitration_with_corroboration(self):
        job = {
            "id": "job_4",
            "title": "Software Engineer",
            "remote_flag": True,
            "job_type": "contract",
            "country": "US",
            "corroborating_tiers_count": 2,  # Multi-tier agreement => INCLUDE
        }
        included = self.guard.check_3_final_arbitration(job, "Disagreement in desc")
        self.assertTrue(included)

    def test_check_3_arbitration_without_corroboration(self):
        job = {
            "id": "job_5",
            "title": "Software Engineer",
            "remote_flag": True,
            "job_type": "contract",
            "country": "US",
            "corroborating_tiers_count": 1,  # Single-tier only => EXCLUDE
        }
        included = self.guard.check_3_final_arbitration(job, "Disagreement in desc")
        self.assertFalse(included)


if __name__ == "__main__":
    unittest.main()
