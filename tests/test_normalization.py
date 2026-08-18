import unittest
from datetime import datetime, timezone
from pipeline.normalize import (
    normalize_adzuna_job,
    normalize_remotive_job,
    normalize_google_job,
    normalize_greenhouse_job,
    normalize_lever_job,
    normalize_job_type,
    match_canonical_title,
    extract_city_and_country,
)

FIXTURE_RECORDS = [
    # 1. Upwork (Contract marketplace - hardcoded contract)
    {"platform": "upwork", "title": "Senior Python Developer", "raw_job_type": "freelance", "expected_type": "contract", "expected_canonical": "Software Engineer"},
    {"platform": "fiverr", "title": "AI Engineer & LLM Specialist", "raw_job_type": "gig", "expected_type": "contract", "expected_canonical": "AI Engineer"},

    {"platform": "toptal", "title": "DevOps Architect", "raw_job_type": "contract", "expected_type": "contract", "expected_canonical": "DevOps Engineer"},
    {"platform": "freelancer", "title": "Data Scientist for Predictive Model", "raw_job_type": "freelance", "expected_type": "contract", "expected_canonical": "Data Scientist"},
    {"platform": "guru", "title": "ServiceNow Administrator", "raw_job_type": "contract", "expected_type": "contract", "expected_canonical": "ServiceNow Engineer"},
    {"platform": "peopleperhour", "title": "Cloud AWS Migration Specialist", "raw_job_type": "project", "expected_type": "contract", "expected_canonical": "Cloud Engineer / Architect"},
    {"platform": "truelancer", "title": "QA Test Automation Lead", "raw_job_type": "contract", "expected_type": "contract", "expected_canonical": "QA / Test Automation Engineer"},
    {"platform": "contra", "title": "Technical Product Manager", "raw_job_type": "freelance", "expected_type": "contract", "expected_canonical": "Product Manager"},

    # 2. Direct ATS & Remote Boards
    {"platform": "greenhouse", "title": "Site Reliability Engineer", "raw_job_type": "full_time", "expected_type": "full_time", "expected_canonical": "Site Reliability Engineer (SRE)"},
    {"platform": "lever", "title": "Cybersecurity Analyst", "raw_job_type": "full_time", "expected_type": "full_time", "expected_canonical": "Cybersecurity Engineer"},
    {"platform": "remotive", "title": "Data Engineer (Spark/Snowflake)", "raw_job_type": "full_time", "expected_type": "full_time", "expected_canonical": "Data Engineer"},
    {"platform": "remoteok", "title": "Machine Learning Engineer", "raw_job_type": "full_time", "expected_type": "full_time", "expected_canonical": "Machine Learning Engineer"},
]


class TestFieldNormalizationPipeline(unittest.TestCase):
    """
    Unit test suite asserting deterministic normalization of all filter fields across platforms.
    """

    def test_canonical_title_matching(self):
        self.assertEqual(match_canonical_title("Senior Software Engineer - React/Node"), "Software Engineer")
        self.assertEqual(match_canonical_title("Lead Data Scientist"), "Data Scientist")
        self.assertEqual(match_canonical_title("Machine Learning & Deep Learning Specialist"), "Machine Learning Engineer")
        self.assertEqual(match_canonical_title("GenAI / LLM / AI Engineer"), "AI Engineer")
        self.assertEqual(match_canonical_title("DevOps & Platform Infrastructure Lead"), "DevOps Engineer")
        self.assertEqual(match_canonical_title("AWS Cloud Architect"), "Cloud Engineer / Architect")
        self.assertEqual(match_canonical_title("Technical Product Manager (TPM)"), "Product Manager")
        self.assertEqual(match_canonical_title("Senior Data Engineer"), "Data Engineer")
        self.assertEqual(match_canonical_title("QA SDET Test Automation Engineer"), "QA / Test Automation Engineer")
        self.assertEqual(match_canonical_title("Site Reliability Engineer (SRE)"), "Site Reliability Engineer (SRE)")
        self.assertEqual(match_canonical_title("Cybersecurity Engineer"), "Cybersecurity Engineer")
        self.assertEqual(match_canonical_title("ServiceNow Developer"), "ServiceNow Engineer")

    def test_contract_only_portals_hardcoding(self):
        for fix in FIXTURE_RECORDS:
            if fix["platform"] in ["upwork", "fiverr", "toptal", "freelancer", "guru", "peopleperhour", "truelancer", "contra"]:
                normalized_type = normalize_job_type(fix["platform"], fix["raw_job_type"], fix["title"])
                self.assertEqual(normalized_type, "contract", f"Platform {fix['platform']} must hardcode job_type='contract'")

    def test_iso_country_parsing(self):
        _, c_in = extract_city_and_country("Bengaluru, India")
        self.assertEqual(c_in, "IN")

        _, c_us = extract_city_and_country("San Francisco, CA, USA")
        self.assertEqual(c_us, "US")

        _, c_gb = extract_city_and_country("London, United Kingdom")
        self.assertEqual(c_gb, "GB")

        _, c_ca = extract_city_and_country("Toronto, ON, Canada")
        self.assertEqual(c_ca, "CA")

    def test_fixture_normalization_matrix(self):
        for fix in FIXTURE_RECORDS:
            c_title = match_canonical_title(fix["title"])
            self.assertEqual(c_title, fix["expected_canonical"], f"Title '{fix['title']}' should match '{fix['expected_canonical']}'")

            j_type = normalize_job_type(fix["platform"], fix["raw_job_type"], fix["title"])
            self.assertEqual(j_type, fix["expected_type"], f"Platform '{fix['platform']}' job_type should normalize to '{fix['expected_type']}'")


if __name__ == "__main__":
    unittest.main()
