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
            "home_team_id": home.get("teamId"),
            "away_team_id": away.get("teamId"),
            "home_pts": home.get("score", 0) or None,
            "away_pts": away.get("score", 0) or None,
            "game_status_text": g.get("gameStatusText", ""),
        })

    return {"games": games, "label": "Today", "context": "live"}


def _fetch_upcoming() -> dict:
    """Check next 2 days for upcoming games if none today."""
    from nba_api.stats.endpoints import scoreboardv2

    # ScoreboardV2 game_header has team IDs but not abbreviations —
    # map them via the GAMECODE column (format: YYYYMMDD/AWYHOM).
    _TEAM_ID_TO_ABBR: dict[int, str] = {
        1610612737: "ATL", 1610612738: "BOS", 1610612751: "BKN",
        1610612766: "CHA", 1610612741: "CHI", 1610612739: "CLE",
        1610612742: "DAL", 1610612743: "DEN", 1610612765: "DET",
        1610612744: "GSW", 1610612745: "HOU", 1610612754: "IND",
        1610612746: "LAC", 1610612747: "LAL", 1610612763: "MEM",
        1610612748: "MIA", 1610612749: "MIL", 1610612750: "MIN",
        1610612740: "NOP", 1610612752: "NYK", 1610612760: "OKC",
        1610612753: "ORL", 1610612755: "PHI", 1610612756: "PHX",
        1610612757: "POR", 1610612758: "SAC", 1610612759: "SAS",
        1610612761: "TOR", 1610612762: "UTA", 1610612764: "WAS",
    }

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
                home_id = row_dict.get("HOME_TEAM_ID")
                away_id = row_dict.get("VISITOR_TEAM_ID")
                games.append({
                    "home_team_abbr": _TEAM_ID_TO_ABBR.get(home_id, ""),
                    "away_team_abbr": _TEAM_ID_TO_ABBR.get(away_id, ""),
                    "home_team_id": home_id,
                    "away_team_id": away_id,
                    "home_pts": None,
                    "away_pts": None,
                    "game_status_text": row_dict.get("GAME_STATUS_TEXT", ""),
                })

            if games:
                if offset == 1:
                    label = "Tomorrow"
                else:
                    label = target.strftime("%a, %b %-d")
                return {"games": games, "label": label, "context": "upcoming"}
        except Exception:
            logger.error("Failed to fetch upcoming games for %s", date_str, exc_info=True)
            continue

    return {"games": [], "label": "Today", "context": "none"}


def _current_season_pair() -> list[str]:
    """['2026-27', '2025-26'] — the upcoming season first, then the last one.

    NBA seasons roll over in the autumn, so anything before August still
    belongs to the season that started the previous calendar year.
    """
    now = datetime.now()
    start = now.year if now.month >= 8 else now.year - 1
    return [f"{start + 1}-{str(start + 2)[2:]}", f"{start}-{str(start + 1)[2:]}"]


def _game_candidates() -> list[tuple]:
    """Every source that could hold the most recent basketball, in no
    particular order — we pick by actual game date, not by guessing."""
    now = datetime.now()
    upcoming, current = _current_season_pair()
    return [
        ("00", upcoming, "Pre Season", "Preseason"),
        ("00", current, "Playoffs", "Playoffs"),
        ("00", current, "Regular Season", "Regular season"),
        ("00", current, "Pre Season", "Preseason"),
        ("13", str(now.year), None, "Summer League"),
        ("13", str(now.year - 1), None, "Summer League"),
    ]


def _fetch_recent() -> dict:
    """Most recently played games, whatever competition they came from.

    In the offseason there is no live slate, so rather than assuming which
    competition is newest (playoffs? preseason? summer league?), query all of
    them in parallel and take whichever actually has the latest game date.
    """
    from concurrent.futures import ThreadPoolExecutor

    from nba_api.stats.endpoints import leaguegamefinder

    def pull(spec):
        league, season, season_type, label = spec
        try:
            kwargs = dict(season_nullable=season, league_id_nullable=league, timeout=25)
            if season_type:
                kwargs["season_type_nullable"] = season_type
            df = leaguegamefinder.LeagueGameFinder(**kwargs).get_data_frames()[0]
        except Exception:
            logger.warning("Game lookup failed for %s %s", league, season, exc_info=True)
            return None
        if df is None or df.empty:
            return None
        return (str(df["GAME_DATE"].max()), label, df)

    specs = _game_candidates()
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        found = [r for r in pool.map(pull, specs) if r]

    if not found:
        return {"games": [], "label": "No games", "context": "none"}

    latest, label, df = max(found, key=lambda r: r[0])
    slate = df[df["GAME_DATE"].astype(str) == latest]

    # One row per team per game — pair them on GAME_ID
    by_game: dict[str, dict] = {}
    for _, r in slate.iterrows():
        game = by_game.setdefault(r["GAME_ID"], {})
        side = "home" if " vs. " in str(r["MATCHUP"]) else "away"
        game[f"{side}_team_abbr"] = r["TEAM_ABBREVIATION"]
        game[f"{side}_team_id"] = int(r["TEAM_ID"])
        game[f"{side}_pts"] = int(r["PTS"]) if r["PTS"] == r["PTS"] else None

    games = [
        {**g, "game_status_text": "Final"}
        for g in by_game.values()
        if g.get("home_team_abbr") and g.get("away_team_abbr")
    ]
    if not games:
        return {"games": [], "label": "No games", "context": "none"}

    try:
        pretty = datetime.strptime(latest[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        pretty = latest[:10]
    return {"games": games, "label": f"{label} · {pretty}",
            "context": "recent", "kind": label, "played_on": latest[:10]}


async def get_scores() -> dict:
    """Get current scores with caching."""
    global _cache

    now = time.time()
    if _cache and (now - _cache[0]) < _CACHE_TTL:
        return _cache[1]

    # Each stage gets its own guard: the live scoreboard endpoint raises
    # outright in the offseason, and a single try block around all three
    # would skip the fallbacks entirely.
    result = {"games": [], "label": "Today", "context": "none"}
    for fetch in (_fetch_scoreboard_today, _fetch_upcoming, _fetch_recent):
        try:
            staged = await asyncio.to_thread(fetch)
        except Exception:
            logger.warning("Scores stage %s failed", fetch.__name__, exc_info=True)
            continue
        if staged.get("games"):
            result = staged
            break

    _cache = (now, result)
    return result
