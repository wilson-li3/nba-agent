from fastapi import APIRouter

from app.services.scores_service import get_scores

router = APIRouter()


@router.get("/scores")
async def scores():
    return await get_scores()
