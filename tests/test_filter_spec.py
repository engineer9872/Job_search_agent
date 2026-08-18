import unittest
from pydantic import ValidationError
from pipeline.filter_lock import create_locked_filter_spec, FilterSpec


class TestFilterSpecIntegrityLock(unittest.TestCase):
    """
    Part F.1 — Unit tests for the FilterSpec hash-integrity check and immutability.
    """

    def test_filter_spec_hash_generation(self):
        spec = create_locked_filter_spec(
            job_title="Software Engineer",
            platform="adzuna",
            country="US",
            remote_only=True,
            job_type="full_time",
        )
        self.assertTrue(spec.verify_integrity())
        self.assertIsNotNone(spec.integrity_hash)
        self.assertEqual(len(spec.integrity_hash), 64)  # SHA-256 length

    def test_filter_spec_immutability(self):
        spec = create_locked_filter_spec(job_title="Software Engineer")
        with self.assertRaises((TypeError, ValidationError)):
            spec.job_title = "Data Scientist"


    def test_invalid_filter_payload_rejection(self):
        # job_type stays STRICT: an unrecognised employment type is a genuine
        # client error and should surface as a 400.
        with self.assertRaises(ValueError):
            create_locked_filter_spec(job_type="invalid_job_type_xyz")

    def test_unsupported_date_posted_degrades_instead_of_raising(self):
        """
        date_posted is deliberately LENIENT as of the filter-set reduction to
        past_12h / past_24h / past_7d / past_30d.

        Raising here used to hard-fail the whole search with a 400. Now that
        values like "past_10m" have been removed, an old bookmark or a cached
        frontend bundle still sending one must degrade to the default window
        rather than breaking the user's search outright.
        """
        for removed_or_bogus in ["past_10m", "past_45m", "invalid_date_posted_abc"]:
            spec = create_locked_filter_spec(date_posted=removed_or_bogus)
            self.assertEqual(spec.date_posted, "past_7d")
            self.assertTrue(spec.verify_integrity())

        # Canonical values must still pass through untouched.
        for supported in ["past_12h", "past_24h", "past_7d", "past_30d"]:
            spec = create_locked_filter_spec(date_posted=supported)
            self.assertEqual(spec.date_posted, supported)


if __name__ == "__main__":
    unittest.main()
