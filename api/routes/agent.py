import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent import JobSearchAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["AI Agent"])


class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Natural language search prompt or query")
    session_id: Optional[str] = Field("default_session", description="Conversational session identifier")


class AgentChatResponse(BaseModel):
    reply: str
    matched_jobs: list
    intent: dict
    insights: dict
    session_id: str


@router.post("/chat", response_model=AgentChatResponse)
def agent_chat_endpoint(request: AgentChatRequest):
    """
    Conversational AI Agent endpoint for natural language job queries with conversation memory.
    """
    try:
        agent = JobSearchAgent()
        result = agent.process_message(request.message, session_id=request.session_id or "default_session")
        return result
    except Exception as e:
        logger.error(f"AI Agent Endpoint Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
