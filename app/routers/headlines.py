import logging

from fastapi import APIRouter

from app.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/headlines")
async def headlines():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT title, url, source, published_at
                FROM news_articles
                WHERE source IN ('ESPN NBA', 'CBS Sports NBA', 'NBA.com', 'RealGM NBA')
                   OR title ~* '(NBA|trade|free agent|playoff|All.Star|draft lottery)'
                ORDER BY published_at DESC NULLS LAST
                LIMIT 5
            """)
        return {
            "headlines": [
                {
                    "title": r["title"],
                    "url": r["url"],
                    "source": r["source"],
                    "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                }
                for r in rows
            ]
        }
    except Exception:
        logger.error("Failed to fetch headlines", exc_info=True)
        return {"headlines": []}
