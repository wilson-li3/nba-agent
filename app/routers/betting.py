import logging

from fastapi import APIRouter

from app.services.betting_picks_service import get_structured_picks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/betting", tags=["betting"])


@router.get("/picks")
async def picks():
    try:
        return await get_structured_picks()
    except Exception:
        logger.error("Failed to generate betting picks", exc_info=True)
        return {"picks": [], "factor_weights": {}, "meta": {"error": "Failed to generate picks"}}
