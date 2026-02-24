import asyncio
import json
import logging
import re

from app.db import get_pool
from app.prompts.format_game_preview import FORMAT_GAME_PREVIEW_PROMPT
from app.services.llm import chat_completion
from app.services.news_service import answer_news_question

logger = logging.getLogger(__name__)

_UNSAFE_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|EXECUTE)\b",
    re.IGNORECASE,
)


async def _execute_query(pool, sql: str, params: list | None = None) -> list[dict]:
    """Execute a read-only query with timeout, return list of dicts."""
    if _UNSAFE_PATTERN.search(sql):
        return []
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                rows = await asyncio.wait_for(
                    conn.fetch(sql, *(params or [])), timeout=15.0
                )
                return [dict(r) for r in rows]
    except Exception:
        logger.error("Game preview query failed: %s", sql, exc_info=True)
        return []


# ── Query builders ──

def _build_probable_starters_query(team_abbr: str) -> tuple[str, list]:
    """Top 5 players by average minutes in last 21 days (probable starters)."""
    sql = """
SELECT p.display_name, p.position, p.jersey,
       COUNT(*) AS gp,
       ROUND(AVG(s.min)::numeric, 1) AS avg_min,
       ROUND(AVG(s.pts)::numeric, 1) AS ppg,
       ROUND(AVG(s.reb)::numeric, 1) AS rpg,
       ROUND(AVG(s.ast)::numeric, 1) AS apg
FROM player_game_stats s
JOIN players p USING (player_id)
WHERE s.team_abbr = $1
  AND s.season_id = '2024-25'
  AND s.game_date >= CURRENT_DATE - 21
GROUP BY p.player_id, p.display_name, p.position, p.jersey
ORDER BY AVG(s.min) DESC
LIMIT 5;
"""
    return sql, [team_abbr]


def _build_season_averages_query(team_abbr: str) -> tuple[str, list]:
    """Season averages for top players on a team."""
    sql = """
SELECT m.display_name, m.games_played, m.ppg, m.rpg, m.apg, m.spg, m.bpg,
       m.fg_pct, m.fg3_pct, m.ft_pct
FROM mv_player_season_averages m
JOIN players p ON p.player_id = m.player_id
JOIN teams t ON t.team_id = p.team_id
WHERE t.abbreviation = $1
  AND m.season_id = '2024-25'
ORDER BY m.ppg DESC
LIMIT 8;
"""
    return sql, [team_abbr]


def _build_h2h_query(team_abbr: str, opponent_abbr: str) -> tuple[str, list]:
    """Head-to-head results this season (team-level aggregation)."""
    sql = """
SELECT s.game_date,
       SUM(s.pts) AS team_pts,
       s.matchup,
       CASE WHEN s.matchup LIKE '%vs.%' THEN 'Home' ELSE 'Away' END AS location
FROM player_game_stats s
WHERE s.team_abbr = $1
  AND s.matchup LIKE '%' || $2 || '%'
  AND s.season_id = '2024-25'
GROUP BY s.game_id, s.game_date, s.matchup
ORDER BY s.game_date DESC;
"""
    return sql, [team_abbr, opponent_abbr]


def _build_defense_query(team_abbr: str) -> tuple[str, list]:
    """Defensive ratings for a team."""
    sql = """
SELECT * FROM mv_team_defensive_ratings WHERE team_abbr = $1;
"""
    return sql, [team_abbr]


def _build_prop_picks_query(team_abbr: str) -> tuple[str, list]:
    """High hit-rate prop picks for players on a specific team."""
    sql = """
SELECT m.display_name,
       m.games_last_10,
       m.pts_25_hit_last10, m.pts_20_hit_last10, m.pts_15_hit_last10,
       m.reb_8_hit_last10, m.reb_6_hit_last10,
       m.ast_6_hit_last10, m.ast_4_hit_last10,
       m.fg3m_3_hit_last10, m.fg3m_2_hit_last10,
       m.pra_40_hit_last10, m.pra_30_hit_last10,
       m.avg_pts_last10, m.avg_reb_last10, m.avg_ast_last10,
       m.avg_fg3m_last10, m.avg_pra_last10,
       m.stddev_pts_last10
FROM mv_player_prop_hit_rates m
JOIN players p ON p.player_id = m.player_id
JOIN teams t ON t.team_id = p.team_id
WHERE t.abbreviation = $1
  AND m.games_last_10 >= 5
ORDER BY m.avg_pts_last10 DESC
LIMIT 10;
"""
    return sql, [team_abbr]


def _build_trend_query(team_abbr: str) -> tuple[str, list]:
    """Last 5 game averages for top players on a team."""
    sql = """
WITH recent AS (
    SELECT s.player_id, p.display_name,
           s.pts, s.reb, s.ast, s.fg3m,
           ROW_NUMBER() OVER (PARTITION BY s.player_id ORDER BY s.game_date DESC) AS rn
    FROM player_game_stats s
    JOIN players p USING (player_id)
    JOIN teams t ON t.team_id = p.team_id
    WHERE t.abbreviation = $1
      AND s.season_id = '2024-25'
)
SELECT display_name,
       ROUND(AVG(pts) FILTER (WHERE rn <= 5)::numeric, 1) AS last_5_ppg,
       ROUND(AVG(pts)::numeric, 1) AS season_ppg,
       ROUND(AVG(reb) FILTER (WHERE rn <= 5)::numeric, 1) AS last_5_rpg,
       ROUND(AVG(reb)::numeric, 1) AS season_rpg,
       ROUND(AVG(ast) FILTER (WHERE rn <= 5)::numeric, 1) AS last_5_apg,
       ROUND(AVG(ast)::numeric, 1) AS season_apg
FROM recent
GROUP BY player_id, display_name
ORDER BY AVG(pts) DESC
LIMIT 6;
"""
    return sql, [team_abbr]


def _build_splits_query(team_abbr: str) -> tuple[str, list]:
    """Home/away splits for key players on a team."""
    sql = """
SELECT m.display_name, m.location, m.games, m.ppg, m.rpg, m.apg, m.fg_pct
FROM mv_player_home_away_splits m
JOIN players p ON p.player_id = m.player_id
JOIN teams t ON t.team_id = p.team_id
WHERE t.abbreviation = $1
ORDER BY m.ppg DESC
LIMIT 12;
"""
    return sql, [team_abbr]


async def generate_game_preview(
    home_team_abbr: str,
    away_team_abbr: str,
    home_team_id: int | None = None,
    away_team_id: int | None = None,
) -> dict:
    """Gather data from multiple domains in parallel and produce a formatted game preview."""
    pool = await get_pool()

    # Build all queries
    queries: dict[str, tuple[str, list]] = {
        "home_starters": _build_probable_starters_query(home_team_abbr),
        "away_starters": _build_probable_starters_query(away_team_abbr),
        "home_season_avg": _build_season_averages_query(home_team_abbr),
        "away_season_avg": _build_season_averages_query(away_team_abbr),
        "h2h_home": _build_h2h_query(home_team_abbr, away_team_abbr),
        "h2h_away": _build_h2h_query(away_team_abbr, home_team_abbr),
        "home_defense": _build_defense_query(home_team_abbr),
        "away_defense": _build_defense_query(away_team_abbr),
        "home_props": _build_prop_picks_query(home_team_abbr),
        "away_props": _build_prop_picks_query(away_team_abbr),
        "home_trends": _build_trend_query(home_team_abbr),
        "away_trends": _build_trend_query(away_team_abbr),
        "home_splits": _build_splits_query(home_team_abbr),
        "away_splits": _build_splits_query(away_team_abbr),
    }

    # Execute all DB queries + news search in parallel
    keys = list(queries.keys())
    news_query = f"{away_team_abbr} vs {home_team_abbr}"

    db_results, news_result = await asyncio.gather(
        asyncio.gather(*[_execute_query(pool, *queries[k]) for k in keys]),
        _safe_news_search(news_query),
    )

    collected_data: dict = {}
    for k, r in zip(keys, db_results):
        collected_data[k] = r

    if news_result:
        collected_data["news"] = news_result

    # Format with LLM
    data_str = json.dumps(collected_data, default=str, indent=2)
    format_prompt = FORMAT_GAME_PREVIEW_PROMPT.format(
        home_team=home_team_abbr,
        away_team=away_team_abbr,
        data=data_str,
    )
    answer = await chat_completion(
        messages=[{"role": "user", "content": format_prompt}],
        model="gpt-4o",
        temperature=0.3,
        max_tokens=3000,
    )

    # Collect news sources if available
    sources = news_result.get("sources", []) if news_result else []

    return {
        "answer": answer,
        "category": "game_preview",
        "sources": sources,
    }


async def _safe_news_search(query: str) -> dict | None:
    """Run news search, returning None on failure."""
    try:
        return await answer_news_question(query)
    except Exception:
        logger.warning("News search failed for game preview: %s", query, exc_info=True)
        return None
