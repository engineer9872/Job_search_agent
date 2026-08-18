import os
import json
import unittest


class TestPortalConfigValidation(unittest.TestCase):
    """
    Automated build/deploy-time config validation test suite.
    Asserts all 10 platforms have at least one viable non-N/A scraping tier configured.
    Updated from the old 30-platform config to the new 10-platform config.
    """

    def setUp(self):
        cfg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "portals_config.json"))
        with open(cfg_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self.portals = {p["id"]: p for p in self.data.get("portals", [])}

    # ------------------------------------------------------------------
    # Platform count
    # ------------------------------------------------------------------
    def test_total_platform_count(self):
        """Config must contain exactly 10 active platforms."""
        self.assertEqual(
            len(self.portals),
            10,
            f"Config must contain exactly 10 platforms, found {len(self.portals)}: {list(self.portals.keys())}"
        )

    def test_expected_platform_ids_present(self):
        """All 10 expected platform IDs must be present."""
        expected = {
            "linkedin", "indeed", "glassdoor",
            "dice", "ziprecruiter", "usajobs", "careerbuilder", "simplyhired",
            "weworkremotely", "hired",
        }
        found = set(self.portals.keys())
        self.assertEqual(
            found,
            expected,
            f"Platform ID mismatch.\nExpected: {sorted(expected)}\nFound: {sorted(found)}"
        )

    # ------------------------------------------------------------------
    # Critical build gate: every scrapable platform must have ≥1 viable tier
    # (USAJOBS satisfies this via T1 alone; others via their respective working tiers)
    # ------------------------------------------------------------------
    def test_at_least_one_viable_tier_per_scrapable_platform(self):
        for p_id, p in self.portals.items():
            viable_tiers = []
            if p.get("t1", {}).get("enabled"):
                viable_tiers.append("T1")
            if p.get("t2", {}).get("enabled"):
                viable_tiers.append("T2")
            if p.get("t3", {}).get("enabled"):
                viable_tiers.append("T3")
            if p.get("t4", {}).get("enabled"):
                viable_tiers.append("T4")
            if p.get("t5", {}).get("enabled"):
                viable_tiers.append("T5")

            self.assertGreater(
                len(viable_tiers),
                0,
                f"LOUD BUILD FAILURE: Platform '{p_id}' ({p.get('name')}) has ZERO viable scraping "
                f"tiers configured! Must have at least one non-N/A tier."
            )

    # ------------------------------------------------------------------
    # ToS-restricted platforms: LinkedIn, Indeed, Glassdoor must have T4 enabled
    # as the practical substitute — no direct scraping allowed
    # ------------------------------------------------------------------
    def test_tos_restricted_platforms_have_t4(self):
        """LinkedIn, Indeed, Glassdoor must have T4 (SerpApi) enabled as practical substitute."""
        for tos_id in ["linkedin", "indeed", "glassdoor"]:
            self.assertIn(tos_id, self.portals, f"Platform '{tos_id}' must be in portals_config.json")
            p = self.portals[tos_id]
            self.assertFalse(
                p.get("direct_scrape", True),
                f"Platform '{tos_id}' must have direct_scrape: false"
            )
            self.assertTrue(
                p.get("t4", {}).get("enabled"),
                f"Platform '{tos_id}' must have T4 enabled (SerpApi Google Jobs aggregation)"
            )
            t2_reason = p.get("t2", {}).get("reason", "")
            t3_reason = p.get("t3", {}).get("reason", "")
            self.assertIn(
                "ToS-restricted",
                t2_reason + t3_reason,
                f"Platform '{tos_id}' T2/T3 must carry reason: 'ToS-restricted'"
            )

    def test_linkedin_direct_scraping_protection(self):
        """LinkedIn must have direct_scrape: false (backward-compat test preserved from old suite)."""
        linkedin_cfg = self.portals.get("linkedin", {})
        self.assertFalse(
            linkedin_cfg.get("direct_scrape", True),
            "LinkedIn must have direct_scrape: false"
        )
        self.assertTrue(
            linkedin_cfg.get("t4", {}).get("enabled"),
            "LinkedIn must enable T4 secondary aggregator routing"
        )

    # ------------------------------------------------------------------
    # USAJOBS eligibility tagging
    # ------------------------------------------------------------------
    def test_usajobs_eligibility_note(self):
        """USAJOBS must have country_code='US' and an eligibility_note per spec."""
        usajobs = self.portals.get("usajobs", {})
        self.assertIn("usajobs", self.portals, "USAJOBS must be in portals_config.json")
        self.assertEqual(
            usajobs.get("country_code"),
            "US",
            "USAJOBS must have country_code: 'US'"
        )
        eligibility = usajobs.get("eligibility_note", "")
        self.assertIn(
            "citizenship",
            eligibility.lower(),
            "USAJOBS eligibility_note must mention citizenship requirement"
        )

    def test_usajobs_t1_is_sole_source(self):
        """USAJOBS T1 must be enabled; T2/T3/T4 must not be needed (per spec)."""
        usajobs = self.portals.get("usajobs", {})
        self.assertTrue(usajobs.get("t1", {}).get("enabled"), "USAJOBS T1 (official API) must be enabled")
        # T2/T3/T4 should all be disabled — T1 fully covers this source
        self.assertFalse(usajobs.get("t2", {}).get("enabled"), "USAJOBS T2 should be disabled (T1 is sole source)")
        self.assertFalse(usajobs.get("t3", {}).get("enabled"), "USAJOBS T3 should be disabled (T1 is sole source)")
        self.assertFalse(usajobs.get("t4", {}).get("enabled"), "USAJOBS T4 should be disabled (T1 is sole source)")

    # ------------------------------------------------------------------
    # Hired low-yield tagging
    # ------------------------------------------------------------------
    def test_hired_low_yield_tagging(self):
        """Hired must be tagged data_completeness='limited' and low_yield_platform=true per spec."""
        hired = self.portals.get("hired", {})
        self.assertIn("hired", self.portals, "Hired must be in portals_config.json")
        self.assertEqual(
            hired.get("data_completeness"),
            "limited",
            "Hired must be tagged data_completeness: 'limited'"
        )
        self.assertTrue(
            hired.get("low_yield_platform"),
            "Hired must be tagged low_yield_platform: true"
        )

    # ------------------------------------------------------------------
    # job_type_parsing: per_listing on every platform
    # ------------------------------------------------------------------
    def test_job_type_parsing_per_listing_on_all_platforms(self):
        """Every platform must have job_type_parsing: 'per_listing' — no hardcoded job_types allowed."""
        for p_id, p in self.portals.items():
            self.assertEqual(
                p.get("job_type_parsing"),
                "per_listing",
                f"Platform '{p_id}' must have job_type_parsing: 'per_listing'. "
                f"None of these 10 platforms are contract-only or full-time-only marketplaces."
            )

    # ------------------------------------------------------------------
    # Removed old tests (platforms no longer in config)
    # ------------------------------------------------------------------
    # test_non_scrapable_eor_platforms — removed: Deel and Multiplier not in new config
    # test_flexjobs_teaser_data_completeness — removed: FlexJobs not in new config


if __name__ == "__main__":
    unittest.main()
