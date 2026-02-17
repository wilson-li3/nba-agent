import asyncio
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Module-level cache: (timestamp, data)
_cache: tuple[float, dict] | None = None
_CACHE_TTL = 120  # seconds


def _fetch_scoreboard_today() -> dict:
    """Fetch today's live/completed games using nba_api."""
    from nba_api.live.nba.endpoints import scoreboard

    sb = scoreboard.ScoreBoard()
    data = sb.get_dict()

    games = []
    for g in data.get("scoreboard", {}).get("games", []):
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        games.append({
            "home_team_abbr": home.get("teamTricode", ""),
            "away_team_abbr": away.get("teamTricode", ""),
            "home_pts": home.get("score", 0) or None,
            "away_pts": away.get("score", 0) or None,
            "game_status_text": g.get("gameStatusText", ""),
        })

    return {"games": games, "label": "Today"}


def _fetch_upcoming() -> dict:
    """Check next 2 days for upcoming games if none today."""
    from nba_api.stats.endpoints import scoreboardv2

    today = datetime.now()
    for offset in range(1, 3):
        target = today + timedelta(days=offset)
        date_str = target.strftime("%Y-%m-%d")

        try:
            sb = scoreboardv2.ScoreboardV2(game_date=date_str)
            headers = sb.game_header.get_dict()
            rows = headers.get("data", [])
            col_names = headers.get("headers", [])

            if not rows:
                continue

            games = []
            for row in rows:
                row_dict = dict(zip(col_names, row))
                games.append({
                    "home_team_abbr": row_dict.get("HOME_TEAM_ABBREVIATION", ""),
                    "away_team_abbr": row_dict.get("VISITOR_TEAM_ABBREVIATION", ""),
                    "home_pts": None,
                    "away_pts": None,
                    "game_status_text": row_dict.get("GAME_STATUS_TEXT", ""),
                })

            if games:
                if offset == 1:
                    label = "Tomorrow"
                else:
                    label = target.strftime("%a, %b %-d")
                return {"games": games, "label": label}
        except Exception:
            logger.error("Failed to fetch upcoming games for %s", date_str, exc_info=True)
            continue

    return {"games": [], "label": "Today"}


async def get_scores() -> dict:
    """Get current scores with caching."""
    global _cache

    now = time.time()
    if _cache and (now - _cache[0]) < _CACHE_TTL:
        return _cache[1]

    try:
        result = await asyncio.to_thread(_fetch_scoreboard_today)
        if not result["games"]:
            result = await asyncio.to_thread(_fetch_upcoming)
    except Exception:
        logger.error("Failed to fetch scores", exc_info=True)
        result = {"games": [], "label": "Today"}

    _cache = (now, result)
    return result
