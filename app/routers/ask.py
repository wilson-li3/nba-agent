from fastapi import APIRouter
from pydantic import BaseModel

from app.services.router_service import route_question

router = APIRouter()


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    question: str
    category: str
    answer: str
    sql: str | None = None


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    result = await route_question(req.question)
    return AskResponse(
        question=req.question,
        category=result.get("category", "stats"),
        answer=result["answer"],
        sql=result.get("sql"),
    )
