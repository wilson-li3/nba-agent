import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.game_preview_service import generate_game_preview

logger = logging.getLogger(__name__)

router = APIRouter()


class GamePreviewRequest(BaseModel):
    home_team_abbr: str
    away_team_abbr: str
    home_team_id: int | None = None
    away_team_id: int | None = None


class GamePreviewResponse(BaseModel):
    answer: str
    category: str
    sources: list | None = None


@router.post("/game-preview", response_model=GamePreviewResponse)
async def game_preview(req: GamePreviewRequest):
    try:
        result = await generate_game_preview(
            home_team_abbr=req.home_team_abbr,
            away_team_abbr=req.away_team_abbr,
            home_team_id=req.home_team_id,
            away_team_id=req.away_team_id,
        )
    except Exception:
        logger.error(
            "Game preview failed: %s @ %s",
            req.away_team_abbr, req.home_team_abbr,
            exc_info=True,
        )
        return GamePreviewResponse(
            answer="Sorry, something went wrong generating the game preview. Please try again.",
            category="error",
        )
    return GamePreviewResponse(
        answer=result["answer"],
        category=result.get("category", "game_preview"),
        sources=result.get("sources"),
    )
