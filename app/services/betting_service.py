import asyncio
import json
import logging
import re

from app.db import get_pool
from app.prompts.format_betting import FORMAT_BETTING_PROMPT
from app.prompts.parse_betting import PARSE_BETTING_PROMPT
from app.services.llm import chat_completion
from app.services.scores_service import get_scores

logger = logging.getLogger(__name__)

# Safety check — same pattern as stats_service
_UNSAFE_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|EXECUTE)\b",
    re.IGNORECASE,
)


async def _parse_betting_intent(question: str) -> dict:
    """Extract structured betting intent from the question via LLM."""
    prompt = PARSE_BETTING_PROMPT.format(question=question)
    raw = await chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o-mini",
        temperature=0.0,
        max_tokens=300,
    )
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "type": "FIND_PICKS",
            "players": [],
            "props": [],
            "teams": [],
            "opponent": None,
            "location": None,
        }


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
        logger.error("Betting query failed: %s", sql, exc_info=True)
        return []


def _build_hit_rate_query(player_name: str) -> tuple[str, list]:
    """Query mv_player_prop_hit_rates for a specific player."""
    sql = """
SELECT *
FROM mv_player_prop_hit_rates
WHERE player_id = (
    SELECT player_id FROM players
    WHERE unaccent(display_name) ILIKE unaccent('%' || $1 || '%')
    LIMIT 1
);
"""
    return sql, [player_name]


def _build_trend_query(player_name: str) -> tuple[str, list]:
    """Compare last 5 vs last 15 vs season averages."""
    sql = """
WITH recent AS (
    SELECT pts, reb, ast, fg3m, pts + reb + ast AS pra,
           ROW_NUMBER() OVER (ORDER BY game_date DESC) AS rn
    FROM player_game_stats
    WHERE player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%' || $1 || '%') LIMIT 1)
      AND season_id = '2024-25'
)
SELECT
    ROUND(AVG(pts) FILTER (WHERE rn <= 5)::numeric, 1)  AS last_5_ppg,
    ROUND(AVG(pts) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_ppg,
    ROUND(AVG(pts)::numeric, 1)                          AS season_ppg,
    ROUND(AVG(reb) FILTER (WHERE rn <= 5)::numeric, 1)  AS last_5_rpg,
    ROUND(AVG(reb) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_rpg,
    ROUND(AVG(reb)::numeric, 1)                          AS season_rpg,
    ROUND(AVG(ast) FILTER (WHERE rn <= 5)::numeric, 1)  AS last_5_apg,
    ROUND(AVG(ast) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_apg,
    ROUND(AVG(ast)::numeric, 1)                          AS season_apg,
    ROUND(AVG(fg3m) FILTER (WHERE rn <= 5)::numeric, 1) AS last_5_fg3mpg,
    ROUND(AVG(fg3m) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_fg3mpg,
    ROUND(AVG(fg3m)::numeric, 1)                         AS season_fg3mpg,
    ROUND(AVG(pra) FILTER (WHERE rn <= 5)::numeric, 1)  AS last_5_pra,
    ROUND(AVG(pra) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_pra,
    ROUND(AVG(pra)::numeric, 1)                          AS season_pra
FROM recent;
"""
    return sql, [player_name]


def _build_splits_query(player_name: str) -> tuple[str, list]:
    """Query mv_player_home_away_splits for a specific player."""
    sql = """
SELECT *
FROM mv_player_home_away_splits
WHERE player_id = (
    SELECT player_id FROM players
    WHERE unaccent(display_name) ILIKE unaccent('%' || $1 || '%')
    LIMIT 1
);
"""
    return sql, [player_name]


def _build_matchup_query(player_name: str, opponent_abbr: str) -> tuple[str, list]:
    """Player's stats vs a specific opponent this season."""
    sql = """
SELECT p.display_name, COUNT(*) AS games,
       ROUND(AVG(s.pts)::numeric, 1) AS ppg,
       ROUND(AVG(s.reb)::numeric, 1) AS rpg,
       ROUND(AVG(s.ast)::numeric, 1) AS apg,
       ROUND(AVG(s.fg3m)::numeric, 1) AS fg3mpg
FROM player_game_stats s
JOIN players p USING (player_id)
WHERE unaccent(p.display_name) ILIKE unaccent('%' || $1 || '%')
  AND s.matchup LIKE '%' || $2 || '%'
  AND s.season_id = '2024-25'
GROUP BY p.display_name;
"""
    return sql, [player_name, opponent_abbr]


def _build_opp_defense_query(opponent_abbr: str) -> tuple[str, list]:
    """Query mv_team_defensive_ratings for a specific opponent."""
    sql = """
SELECT *
FROM mv_team_defensive_ratings
WHERE team_abbr = $1;
"""
    return sql, [opponent_abbr]


def _build_find_picks_query() -> tuple[str, list]:
    """Find players with high prop hit rates (>=80% over last 10)."""
    sql = """
SELECT display_name,
       pts_25_hit_last10, pts_20_hit_last10, pts_15_hit_last10,
       reb_8_hit_last10, reb_6_hit_last10,
       ast_6_hit_last10, ast_4_hit_last10,
       fg3m_3_hit_last10, fg3m_2_hit_last10,
       pra_40_hit_last10, pra_30_hit_last10,
       games_last_10,
       avg_pts_last10, avg_reb_last10, avg_ast_last10, avg_fg3m_last10, avg_pra_last10,
       stddev_pts_last10
FROM mv_player_prop_hit_rates
WHERE games_last_10 >= 8
  AND (
    pts_25_hit_last10 >= 8
    OR pts_20_hit_last10 >= 8
    OR reb_8_hit_last10 >= 8
    OR ast_6_hit_last10 >= 8
    OR fg3m_3_hit_last10 >= 8
    OR pra_40_hit_last10 >= 8
  )
ORDER BY
    GREATEST(
        pts_25_hit_last10::float / NULLIF(games_last_10, 0),
        reb_8_hit_last10::float / NULLIF(games_last_10, 0),
        ast_6_hit_last10::float / NULLIF(games_last_10, 0),
        fg3m_3_hit_last10::float / NULLIF(games_last_10, 0)
    ) DESC
LIMIT 15;
"""
    return sql, []


async def _get_todays_schedule() -> dict[str, dict]:
    """Return {team_abbr: {"opponent": opp_abbr, "location": "home"|"away"}} for today's games."""
    try:
        scores = await get_scores()
    except Exception:
        logger.warning("Could not fetch today's schedule", exc_info=True)
        return {}
    schedule: dict[str, dict] = {}
    for g in scores.get("games", []):
        home = g.get("home_team_abbr")
        away = g.get("away_team_abbr")
        if home and away:
            schedule[home] = {"opponent": away, "location": "home"}
            schedule[away] = {"opponent": home, "location": "away"}
    return schedule


def _build_player_team_query(player_names: list[str]) -> tuple[str, list]:
    """Batch-resolve player names to their current team abbreviations."""
    placeholders = ", ".join(f"unaccent('%' || ${i+1} || '%')" for i in range(len(player_names)))
    sql = f"""
SELECT p.display_name, t.abbreviation AS team_abbr
FROM players p
JOIN teams t ON t.team_id = p.team_id
WHERE unaccent(p.display_name) ILIKE ANY(ARRAY[{placeholders}]);
"""
    return sql, player_names


def _build_b2b_today_query() -> tuple[str, list]:
    """Find teams that played yesterday (potential B2B if also playing today)."""
    sql = """
SELECT DISTINCT t.abbreviation AS team_abbr
FROM mv_team_back_to_backs b
JOIN teams t ON t.team_id = b.team_id
WHERE b.game_date = CURRENT_DATE - 1;
"""
    return sql, []


# --- Team abbreviation mapping for opponent resolution ---
TEAM_ABBR_MAP = {
    "hawks": "ATL", "celtics": "BOS", "nets": "BKN", "hornets": "CHA",
    "bulls": "CHI", "cavaliers": "CLE", "cavs": "CLE", "mavericks": "DAL",
    "mavs": "DAL", "nuggets": "DEN", "pistons": "DET", "warriors": "GSW",
    "rockets": "HOU", "pacers": "IND", "clippers": "LAC", "lakers": "LAL",
    "grizzlies": "MEM", "heat": "MIA", "bucks": "MIL", "timberwolves": "MIN",
    "wolves": "MIN", "pelicans": "NOP", "knicks": "NYK", "thunder": "OKC",
    "magic": "ORL", "76ers": "PHI", "sixers": "PHI", "suns": "PHX",
    "blazers": "POR", "trail blazers": "POR", "kings": "SAC", "spurs": "SAS",
    "raptors": "TOR", "jazz": "UTA", "wizards": "WAS",
    # Already abbreviations
    "atl": "ATL", "bos": "BOS", "bkn": "BKN", "cha": "CHA", "chi": "CHI",
    "cle": "CLE", "dal": "DAL", "den": "DEN", "det": "DET", "gsw": "GSW",
    "hou": "HOU", "ind": "IND", "lac": "LAC", "lal": "LAL", "mem": "MEM",
    "mia": "MIA", "mil": "MIL", "min": "MIN", "nop": "NOP", "nyk": "NYK",
    "okc": "OKC", "orl": "ORL", "phi": "PHI", "phx": "PHX", "por": "POR",
    "sac": "SAC", "sas": "SAS", "tor": "TOR", "uta": "UTA", "was": "WAS",
}


def _resolve_team_abbr(team_str: str | None) -> str | None:
    """Convert team name/abbreviation to standard 3-letter abbreviation."""
    if not team_str:
        return None
    return TEAM_ABBR_MAP.get(team_str.lower().strip())


def _detect_parlay_correlations(props: list[dict]) -> list[str]:
    """Detect correlations between parlay legs using rule-based logic."""
    warnings = []
    if len(props) < 2:
        return warnings

    # Group by player
    players = {}
    for p in props:
        name = p.get("player", "")
        players.setdefault(name, []).append(p)

    # Same player, multiple stats → positively correlated
    for name, player_props in players.items():
        if len(player_props) > 1:
            stats = [p.get("stat") for p in player_props]
            if "pts" in stats and "ast" in stats:
                warnings.append(
                    f"{name}: points and assists are positively correlated — "
                    "high-usage games boost both. This helps if both are overs."
                )

    # Check for same-team players
    # We can't fully resolve teams here, but flag if multiple legs exist
    player_names = [p.get("player", "") for p in props]
    if len(player_names) > len(set(player_names)):
        pass  # Already handled above

    if len(props) >= 2:
        over_pts_count = sum(
            1 for p in props
            if p.get("stat") == "pts" and p.get("direction", "over") == "over"
        )
        if over_pts_count >= 2:
            warnings.append(
                "Multiple players over on points — if they're on the same team, "
                "scoring is somewhat zero-sum within finite possessions. "
                "If on opposing teams, a blowout could limit one player's minutes."
            )

    if len(props) >= 3:
        warnings.append(
            f"This is a {len(props)}-leg parlay. Combined probability drops "
            "significantly with each leg — even 70% individual legs give only "
            f"~{round(0.7 ** len(props) * 100)}% combined."
        )

    return warnings


async def answer_betting_question(
    question: str, news_context: str | None = None
) -> dict:
    """Full betting analysis pipeline: parse intent, run parallel queries, synthesize."""
    # Step 1: Parse intent
    intent = await _parse_betting_intent(question)
    intent_type = intent.get("type", "FIND_PICKS")
    players = intent.get("players", [])
    props = intent.get("props", [])
    opponent_raw = intent.get("opponent")
    teams_raw = intent.get("teams", [])

    # Resolve opponent abbreviation
    opponent_abbr = _resolve_team_abbr(opponent_raw)
    if not opponent_abbr and teams_raw:
        # Try to find opponent from teams list
        for t in teams_raw:
            abbr = _resolve_team_abbr(t)
            if abbr:
                opponent_abbr = abbr
                break

    pool = await get_pool()
    collected_data = {}

    # Step 2: Build and execute queries based on intent type
    if intent_type == "PROP_CHECK" and players:
        player_name = players[0]
        queries = {"hit_rate": _build_hit_rate_query(player_name)}
        queries["trend"] = _build_trend_query(player_name)
        queries["splits"] = _build_splits_query(player_name)
        if opponent_abbr:
            queries["matchup"] = _build_matchup_query(player_name, opponent_abbr)
            queries["opp_defense"] = _build_opp_defense_query(opponent_abbr)

        # Execute all in parallel — each value is (sql, params)
        keys = list(queries.keys())
        results = await asyncio.gather(
            *[_execute_query(pool, *queries[k]) for k in keys]
        )
        for k, r in zip(keys, results):
            collected_data[k] = r

        # Add prop context
        if props:
            collected_data["requested_prop"] = props[0]

    elif intent_type == "FIND_PICKS":
        if players:
            # Player-specific picks — gather data + auto-detect opponents
            # Pre-fetch context in parallel
            schedule_task = asyncio.create_task(_get_todays_schedule())
            b2b_sql, b2b_params = _build_b2b_today_query()
            b2b_task = asyncio.create_task(_execute_query(pool, b2b_sql, b2b_params))
            pt_sql, pt_params = _build_player_team_query(players[:3])
            pt_task = asyncio.create_task(_execute_query(pool, pt_sql, pt_params))

            schedule, b2b_rows, pt_rows = await asyncio.gather(
                schedule_task, b2b_task, pt_task
            )
            b2b_teams = {r["team_abbr"] for r in b2b_rows}
            player_team_map = {r["display_name"]: r["team_abbr"] for r in pt_rows}

            collected_data["todays_schedule"] = schedule
            collected_data["teams_on_b2b"] = list(b2b_teams)
            collected_data["player_teams"] = player_team_map

            # Collect all unique opponents for defense queries
            opp_set: set[str] = set()
            if opponent_abbr:
                opp_set.add(opponent_abbr)

            for player_name in players[:3]:
                prefix = player_name.replace(" ", "_")
                queries = {
                    f"{prefix}_hit_rate": _build_hit_rate_query(player_name),
                    f"{prefix}_trend": _build_trend_query(player_name),
                    f"{prefix}_splits": _build_splits_query(player_name),
                }
                # Use explicit opponent or auto-detect from schedule
                effective_opp = opponent_abbr
                if not effective_opp:
                    team = player_team_map.get(player_name)
                    if team and team in schedule:
                        effective_opp = schedule[team]["opponent"]
                if effective_opp:
                    queries[f"{prefix}_matchup"] = _build_matchup_query(player_name, effective_opp)
                    opp_set.add(effective_opp)

                keys = list(queries.keys())
                results = await asyncio.gather(
                    *[_execute_query(pool, *queries[k]) for k in keys]
                )
                for k, r in zip(keys, results):
                    collected_data[k] = r

            # Fetch opponent defense for all relevant opponents in parallel
            if opp_set:
                opp_list = list(opp_set)
                def_results = await asyncio.gather(
                    *[_execute_query(pool, *_build_opp_defense_query(o)) for o in opp_list]
                )
                collected_data["opponent_defense"] = {
                    opp: res for opp, res in zip(opp_list, def_results) if res
                }
        else:
            # League-wide scan (no player specified)
            picks_sql, picks_params = _build_find_picks_query()
            b2b_sql, b2b_params = _build_b2b_today_query()

            # Parallel: hit rates + schedule + B2B
            picks_task = asyncio.create_task(_execute_query(pool, picks_sql, picks_params))
            schedule_task = asyncio.create_task(_get_todays_schedule())
            b2b_task = asyncio.create_task(_execute_query(pool, b2b_sql, b2b_params))

            pick_results, schedule, b2b_rows = await asyncio.gather(
                picks_task, schedule_task, b2b_task
            )
            collected_data["high_hit_rate_props"] = pick_results
            collected_data["todays_schedule"] = schedule
            collected_data["teams_on_b2b"] = [r["team_abbr"] for r in b2b_rows]

            # Resolve player names from pick results to teams
            if pick_results:
                player_names = [r["display_name"] for r in pick_results]
                pt_sql, pt_params = _build_player_team_query(player_names)
                pt_rows = await _execute_query(pool, pt_sql, pt_params)
                player_team_map = {r["display_name"]: r["team_abbr"] for r in pt_rows}
                collected_data["player_teams"] = player_team_map

                # Find unique opponents for players playing today
                opp_set: set[str] = set()
                for name, team in player_team_map.items():
                    if team in schedule:
                        opp_set.add(schedule[team]["opponent"])

                # Fetch opponent defense in parallel
                if opp_set:
                    opp_list = list(opp_set)
                    def_results = await asyncio.gather(
                        *[_execute_query(pool, *_build_opp_defense_query(o)) for o in opp_list]
                    )
                    collected_data["opponent_defense"] = {
                        opp: res for opp, res in zip(opp_list, def_results) if res
                    }

    elif intent_type == "PARLAY" and props:
        # Analyze each leg in parallel
        all_queries = {}
        for i, prop in enumerate(props):
            player_name = prop.get("player", "")
            if not player_name:
                continue
            prefix = f"leg{i}"
            all_queries[f"{prefix}_hit_rate"] = _build_hit_rate_query(player_name)
            all_queries[f"{prefix}_trend"] = _build_trend_query(player_name)

        if opponent_abbr:
            all_queries["opp_defense"] = _build_opp_defense_query(opponent_abbr)

        keys = list(all_queries.keys())
        results = await asyncio.gather(
            *[_execute_query(pool, *all_queries[k]) for k in keys]
        )
        for k, r in zip(keys, results):
            collected_data[k] = r

        collected_data["parlay_legs"] = props
        collected_data["correlation_warnings"] = _detect_parlay_correlations(props)

    elif intent_type == "GAME_PREVIEW":
        queries = {}
        # Get defensive ratings for teams involved
        for t in teams_raw:
            abbr = _resolve_team_abbr(t)
            if abbr:
                queries[f"defense_{abbr}"] = _build_opp_defense_query(abbr)
        # Get key player trends if any players mentioned
        for p in players[:3]:
            queries[f"trend_{p}"] = _build_trend_query(p)
            queries[f"hits_{p}"] = _build_hit_rate_query(p)

        if queries:
            keys = list(queries.keys())
            results = await asyncio.gather(
                *[_execute_query(pool, *queries[k]) for k in keys]
            )
            for k, r in zip(keys, results):
                collected_data[k] = r
    else:
        # Fallback: treat as FIND_PICKS
        sql, params = _build_find_picks_query()
        results = await _execute_query(pool, sql, params)
        collected_data["high_hit_rate_props"] = results

    # Add news context if available
    if news_context:
        collected_data["news_context"] = news_context

    # Step 3: Format with betting-specific prompt
    data_str = json.dumps(collected_data, default=str, indent=2)
    format_prompt = FORMAT_BETTING_PROMPT.format(question=question, data=data_str)
    answer = await chat_completion(
        messages=[{"role": "user", "content": format_prompt}],
        model="gpt-4o",
        temperature=0.2,
    )

    return {
        "answer": answer,
        "intent": intent,
    }
