import re
import time
import logging
from typing import Dict, Any, List, Optional
from agent.tools import search_jobs_tool, get_job_details_tool, get_market_insights_tool

logger = logging.getLogger(__name__)

# Session TTL (30 minutes idle timeout) & Capacity Bounds
SESSION_TTL_SECONDS = 1800  # 30 minutes
MAX_SESSIONS = 1000

# In-memory dictionary tracking session state: { session_id: { "intent": dict, "last_accessed": float } }
SESSION_MEMORY: Dict[str, Dict[str, Any]] = {}


def _cleanup_expired_sessions():
    """
    Purges sessions that have exceeded the idle TTL (30 minutes) or enforces MAX_SESSIONS capacity.
    """
    now = time.time()
    expired_ids = [
        sid for sid, data in SESSION_MEMORY.items()
        if now - data.get("last_accessed", 0) > SESSION_TTL_SECONDS
    ]
    for sid in expired_ids:
        del SESSION_MEMORY[sid]
        logger.info(f"[SessionMemory] Expired session '{sid}' purged after 30 minutes idle.")

    # LRU Eviction if capacity exceeded
    if len(SESSION_MEMORY) > MAX_SESSIONS:
        sorted_sessions = sorted(SESSION_MEMORY.items(), key=lambda item: item[1].get("last_accessed", 0))
        to_remove = len(SESSION_MEMORY) - MAX_SESSIONS
        for sid, _ in sorted_sessions[:to_remove]:
            del SESSION_MEMORY[sid]
            logger.info(f"[SessionMemory] Capacity cap reached. Evicted oldest session '{sid}'.")


def get_session_intent(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves session intent if valid and not expired."""
    _cleanup_expired_sessions()
    session_data = SESSION_MEMORY.get(session_id)
    if session_data:
        session_data["last_accessed"] = time.time()
        return session_data.get("intent")
    return None


def save_session_intent(session_id: str, intent: Dict[str, Any]):
    """Saves updated intent for a session and refreshes last_accessed timestamp."""
    _cleanup_expired_sessions()
    SESSION_MEMORY[session_id] = {
        "intent": intent,
        "last_accessed": time.time(),
    }


def clear_session(session_id: str):
    """Explicitly clears memory for a session."""
    if session_id in SESSION_MEMORY:
        del SESSION_MEMORY[session_id]


def parse_query_intent(prompt: str, last_intent: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Parses user natural language query to extract search intent and filter parameters.
    Supports conversational memory inheritance and detail requests ('give me the details in chat').
    """
    p_lower = prompt.lower()

    # Detect conversational detail requests
    detail_triggers = [
        "detail", "details", "here in chat", "in chat", "give me details", "show details",
        "tell me more", "list them", "list jobs", "explain", "summarize"
    ]
    is_detail_request = any(trig in p_lower for trig in detail_triggers) and last_intent is not None

    # Detect follow-up phrases referencing previous results
    follow_up_triggers = ["from that", "from those", "from them", "only remote", "narrow down", "filter by", "those", "these", "only"]
    is_follow_up = (any(trig in p_lower for trig in follow_up_triggers) or is_detail_request) and last_intent is not None

    # Remote detection
    remote_only = any(term in p_lower for term in ["remote", "work from home", "wfh", "telecommute"])
    if is_follow_up and not remote_only:
        remote_only = last_intent.get("remote_only", False)

    # Country detection
    country = None
    if "india" in p_lower:
        country = "IN"
    elif "uk" in p_lower or "united kingdom" in p_lower or "london" in p_lower:
        country = "GB"
    elif "canada" in p_lower:
        country = "CA"
    elif "us" in p_lower or "united states" in p_lower or "america" in p_lower:
        country = "US"
    elif is_follow_up:
        country = last_intent.get("country")

    # Salary extraction e.g. "$120k", "150k", "120000"
    min_salary = None
    salary_match = re.search(r"(?:over|above|min|\$|\>)?\s*(\d{2,3})\s*k\b", p_lower)
    if salary_match:
        min_salary = float(salary_match.group(1)) * 1000.0
    else:
        num_match = re.search(r"(?:over|above|min|\$)\s*(\d{5,7})\b", p_lower)
        if num_match:
            min_salary = float(num_match.group(1))
        elif is_follow_up:
            min_salary = last_intent.get("min_salary")

    # Platform detection
    platform = None
    if "greenhouse" in p_lower:
        platform = "greenhouse"
    elif "lever" in p_lower:
        platform = "lever"
    elif "adzuna" in p_lower:
        platform = "adzuna"
    elif "jsearch" in p_lower:
        platform = "jsearch"
    elif "google" in p_lower:
        platform = "google_jobs"
    elif is_follow_up:
        platform = last_intent.get("platform")

    # Extracted keyword / query term
    clean_words = []
    stopwords = {
        "find", "me", "show", "jobs", "job", "roles", "role", "positions", "paying", "over", "above",
        "remote", "in", "the", "for", "with", "at", "a", "an", "from", "that", "those", "them", "only",
        "ones", "give", "details", "here", "chat", "tell", "more", "list", "explain"
    }
    for word in re.findall(r"\b[a-zA-Z]{2,}\b", prompt):
        if word.lower() not in stopwords:
            clean_words.append(word)

    keyword_query = " ".join(clean_words[:4]) if clean_words else None
    if is_follow_up and not keyword_query:
        keyword_query = last_intent.get("query")

    return {
        "query": keyword_query,
        "remote_only": remote_only,
        "country": country,
        "min_salary": min_salary,
        "platform": platform,
        "is_follow_up": is_follow_up,
        "is_detail_request": is_detail_request,
    }


class JobSearchAgent:
    """
    AI Agent with TTL-backed Conversation Session Memory.
    """

    def process_message(self, user_message: str, session_id: str = "default_session") -> Dict[str, Any]:
        """
        Processes user chat message, manages TTL session memory, queries DB tools, and returns response.
        """
        logger.info(f"[JobAgent] Session '{session_id}' processing prompt: '{user_message}'")
        last_intent = get_session_intent(session_id)

        intent = parse_query_intent(user_message, last_intent=last_intent)

        # Save intent with updated timestamp
        save_session_intent(session_id, intent)

        matched_jobs = search_jobs_tool(
            query=intent["query"],
            remote_only=intent["remote_only"],
            country=intent["country"],
            min_salary=intent["min_salary"],
            platform=intent["platform"],
            limit=20,
        )

        insights = get_market_insights_tool(keyword=intent["query"])

        # Construct AI Natural Language Reply
        query_str = f"'{intent['query']}'" if intent['query'] else "all roles"
        remote_str = " (Remote Only)" if intent['remote_only'] else ""
        salary_str = f" paying over ${int(intent['min_salary']):,}" if intent['min_salary'] else ""
        country_str = f" in {intent['country']}" if intent['country'] else ""

        if matched_jobs:
            if intent.get("is_detail_request"):
                details_list = []
                for idx, j in enumerate(matched_jobs[:5], 1):
                    loc = f"{j.get('city') or 'Remote'}, {j.get('country') or 'Global'}"
                    apply_link = j.get('apply_url')
                    link_str = f"[Apply Now]({apply_link})" if apply_link else "No link"
                    desc = j.get('description_snippet') or ''
                    snippet = desc[:150] + "..." if len(desc) > 150 else desc
                    details_list.append(
                        f"**{idx}. {j.get('title')}** at **{j.get('company')}**\n"
                        f"📍 Location: `{loc}` | 🏢 Platform: `{j.get('source_platform')}`\n"
                        f"🔗 {link_str}\n"
                        f"📝 {snippet}\n"
                    )

                reply_header = (
                    f"Here are the details for the top **{min(5, len(matched_jobs))} matching roles** for {query_str}:\n\n"
                    + "\n".join(details_list)
                )
            else:
                reply_header = (
                    f"I found **{len(matched_jobs)} matching job listings** for {query_str}{remote_str}{country_str}{salary_str}.\n\n"
                    f"📊 **Market Insight**: Out of {insights['total_jobs']} relevant listings, **{insights['remote_percentage']}%** offer remote work options."
                )
        else:
            reply_header = (
                f"I searched for {query_str}{remote_str}{country_str}{salary_str}, but no exact matches met all criteria.\n\n"
                f"💡 **Suggestion**: Try loosening your filters or resetting the chat memory."
            )

        return {
            "reply": reply_header,
            "matched_jobs": matched_jobs,
            "intent": intent,
            "insights": insights,
            "session_id": session_id,
        }

