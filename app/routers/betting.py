import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.betting_picks_service import get_structured_picks
from app.services.llm import LLMBudgetExceeded
from app.services.simulation_service import simulate_slip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/betting", tags=["betting"])


class SimLeg(BaseModel):
    player_name: str
    prop_type: str
    line: float
    team: str | None = None
    opponent: str | None = None
    location: str | None = None


class SimulateRequest(BaseModel):
    legs: list[SimLeg] = Field(default_factory=list)
    use_news: bool = True


@router.get("/picks")
async def picks():
    try:
        return await get_structured_picks()
    except Exception:
        logger.error("Failed to generate betting picks", exc_info=True)
        return {"picks": [], "factor_weights": {}, "meta": {"error": "Failed to generate picks"}}


@router.post("/simulate")
async def simulate(req: SimulateRequest):
    try:
        return await simulate_slip([leg.model_dump() for leg in req.legs], use_news=req.use_news)
    except LLMBudgetExceeded:
        logger.warning("Daily LLM budget reached; refusing simulation")
        return {"error": "This demo has reached its daily model budget, so live answers are paused until it resets at midnight UTC."}
    except Exception:
        logger.error("Simulation failed", exc_info=True)
        return {"error": "Simulation failed. Check the server logs."}
