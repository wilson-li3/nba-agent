from fastapi import APIRouter

from app.rate_limit import snapshot as rate_snapshot
from app.services.llm import usage_snapshot

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/usage")
async def usage():
    """Today's model spend and throttle state — watch this after deploying."""
    return {"llm": usage_snapshot(), "rate_limit": rate_snapshot()}
