import time
import unittest
from agent.agent_engine import (
    parse_query_intent,
    JobSearchAgent,
    save_session_intent,
    get_session_intent,
    clear_session,
    SESSION_MEMORY,
    SESSION_TTL_SECONDS,
)
from agent.tools import search_jobs_tool, get_market_insights_tool


class TestAIAgent(unittest.TestCase):

    def test_parse_query_intent(self):
        prompt = "Find me remote senior python developer roles in US paying over $130k"
        intent = parse_query_intent(prompt)

        self.assertTrue(intent["remote_only"])
        self.assertEqual(intent["country"], "US")
        self.assertEqual(intent["min_salary"], 130000.0)
        self.assertIn("python", intent["query"].lower())

    def test_agent_process_message(self):
        agent = JobSearchAgent()
        result = agent.process_message("Show me remote python engineer jobs", session_id="test_sess_1")

        self.assertIn("reply", result)
        self.assertIn("matched_jobs", result)
        self.assertIn("intent", result)
        self.assertIsInstance(result["matched_jobs"], list)

    def test_session_memory_ttl_expiration(self):
        session_id = "ttl_test_session"
        intent_data = {"query": "java", "country": "IN", "remote_only": True}
        save_session_intent(session_id, intent_data)

        # Confirm session saved
        saved = get_session_intent(session_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["query"], "java")

        # Simulate TTL expiration (manually set last_accessed timestamp to 35 mins ago)
        SESSION_MEMORY[session_id]["last_accessed"] = time.time() - (SESSION_TTL_SECONDS + 300)

        # Next retrieval should trigger TTL cleanup and return None
        expired = get_session_intent(session_id)
        self.assertIsNone(expired)

    def test_search_jobs_tool(self):
        jobs = search_jobs_tool(query="python", limit=5)
        self.assertIsInstance(jobs, list)

    def test_market_insights_tool(self):
        insights = get_market_insights_tool("engineer")
        self.assertIn("total_jobs", insights)
        self.assertIn("remote_jobs", insights)


if __name__ == "__main__":
    unittest.main()
