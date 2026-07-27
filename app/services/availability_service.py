"""Availability gate for the picks board.

The prediction engine prices props from historical game logs, so it will
happily rate a player who is injured, suspended, or has changed teams — the
box scores look identical either way. This service reads the ingested news
corpus and lets the board drop anyone who clearly is not playing.

Kept deliberately cheap: one query for the recent corpus, name matching in
Python, then a single batched LLM call for every candidate at once rather
than one call per player.
"""

from __future__ import annotations

import json
import logging
import unicodedata

from app.prompts.assess_availability import ASSESS_AVAILABILITY_PROMPT
from app.services.betting_service import _execute_query
from app.services.llm import chat_completion

logger = logging.getLogger(__name__)

RECENT_DAYS = 21
MAX_SNIPPETS_PER_PLAYER = 3
SNIPPET_CHARS = 320


def _fold(text: str) -> str:
    """Strip accents and lowercase, so 'Dončić' matches 'Doncic' in copy."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


async def _recent_chunks(pool) -> list[dict]:
    sql = f"""
    SELECT nc.content, na.title, na.source, na.url
    FROM news_chunks nc
    JOIN news_articles na ON na.article_id = nc.article_id
    WHERE COALESCE(na.published_at, na.ingested_at) > NOW() - INTERVAL '{RECENT_DAYS} days'
    ORDER BY COALESCE(na.published_at, na.ingested_at) DESC
    LIMIT 600;
    """
    return await _execute_query(pool, sql)


async def check_availability(pool, players: list[str]) -> dict[str, dict]:
    """Return {player: {status, note, sources}} for every name given.

    Players with no matching coverage come back "available" — silence is not
    evidence of an injury, and the board should not drop someone just because
    the news feed is quiet.
    """
    default = {name: {"status": "available", "note": "", "sources": []} for name in players}
    if not players:
        return default

    try:
        chunks = await _recent_chunks(pool)
    except Exception:
        logger.warning("Availability news lookup failed", exc_info=True)
        return default
    if not chunks:
        return default

    # Match each player against the recent corpus
    hits: dict[str, list[dict]] = {}
    folded = [(name, _fold(name)) for name in players]
    for ch in chunks:
        body = _fold(ch.get("content") or "")
        for name, key in folded:
            if key and key in body:
                bucket = hits.setdefault(name, [])
                if len(bucket) < MAX_SNIPPETS_PER_PLAYER:
                    bucket.append(ch)

    if not hits:
        return default

    blocks = []
    for name, chs in hits.items():
        joined = "\n".join(f"  - ({c['source']}) {c['content'][:SNIPPET_CHARS]}" for c in chs)
        blocks.append(f"{name}:\n{joined}")

    prompt = ASSESS_AVAILABILITY_PROMPT.format(players="\n\n".join(blocks))
    try:
        raw = (await chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini", temperature=0.0, max_tokens=700,
        )).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        parsed = json.loads(raw.strip())
    except Exception:
        logger.warning("Availability assessment failed", exc_info=True)
        return default

    result = dict(default)
    for name, verdict in (parsed or {}).items():
        if name not in result or not isinstance(verdict, dict):
            continue
        status = str(verdict.get("status", "available")).lower()
        if status not in ("out", "questionable", "available"):
            status = "available"
        result[name] = {
            "status": status,
            "note": str(verdict.get("note", ""))[:180],
            "sources": [
                {"title": c["title"], "url": c["url"], "source": c["source"]}
                for c in hits.get(name, [])[:2]
            ],
        }
    return result
