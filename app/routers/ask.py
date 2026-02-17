import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.router_service import route_question

logger = logging.getLogger(__name__)

router = APIRouter()


class AskRequest(BaseModel):
    question: str
    message_history: list[dict] | None = None  # [{"role": "user"|"assistant", "content": "..."}]


class AskResponse(BaseModel):
    question: str
    category: str
    answer: str
    sql: str | None = None
    sources: list | None = None


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    try:
        result = await route_question(req.question, message_history=req.message_history)
    except Exception:
        logger.error("Unhandled error for question: %s", req.question, exc_info=True)
        return AskResponse(
            question=req.question,
            category="error",
            answer="Sorry, something went wrong. Please try again in a moment.",
        )
    return AskResponse(
        question=req.question,
        category=result.get("category", "stats"),
        answer=result["answer"],
        sql=result.get("sql"),
        sources=result.get("sources"),
    )
