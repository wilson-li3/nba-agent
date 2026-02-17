import asyncio
import json
import re

from app.db import get_pool
from app.prompts.format_betting import FORMAT_BETTING_PROMPT
from app.prompts.parse_betting import PARSE_BETTING_PROMPT
from app.services.llm import chat_completion

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


async def _execute_query(pool, sql: str) -> list[dict]:
    """Execute a read-only query with timeout, return list of dicts."""
    if _UNSAFE_PATTERN.search(sql):
        return []
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                rows = await asyncio.wait_for(conn.fetch(sql), timeout=15.0)
                return [dict(r) for r in rows]
    except Exception:
        return []


def _build_hit_rate_query(player_name: str) -> str:
    """Query mv_player_prop_hit_rates for a specific player."""
    return f"""
SELECT *
FROM mv_player_prop_hit_rates
WHERE player_id = (
    SELECT player_id FROM players
    WHERE unaccent(display_name) ILIKE unaccent('%{player_name}%')
    LIMIT 1
);
"""


def _build_trend_query(player_name: str) -> str:
    """Compare last 5 vs last 15 vs season averages."""
    return f"""
WITH recent AS (
    SELECT pts, reb, ast, fg3m, pts + reb + ast AS pra,
           ROW_NUMBER() OVER (ORDER BY game_date DESC) AS rn
    FROM player_game_stats
    WHERE player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%{player_name}%') LIMIT 1)
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


def _build_splits_query(player_name: str) -> str:
    """Query mv_player_home_away_splits for a specific player."""
    return f"""
SELECT *
FROM mv_player_home_away_splits
WHERE player_id = (
    SELECT player_id FROM players
    WHERE unaccent(display_name) ILIKE unaccent('%{player_name}%')
    LIMIT 1
);
"""


def _build_matchup_query(player_name: str, opponent_abbr: str) -> str:
    """Player's stats vs a specific opponent this season."""
    return f"""
SELECT p.display_name, COUNT(*) AS games,
       ROUND(AVG(s.pts)::numeric, 1) AS ppg,
       ROUND(AVG(s.reb)::numeric, 1) AS rpg,
       ROUND(AVG(s.ast)::numeric, 1) AS apg,
       ROUND(AVG(s.fg3m)::numeric, 1) AS fg3mpg
FROM player_game_stats s
JOIN players p USING (player_id)
WHERE unaccent(p.display_name) ILIKE unaccent('%{player_name}%')
  AND s.matchup LIKE '%{opponent_abbr}%'
  AND s.season_id = '2024-25'
GROUP BY p.display_name;
"""


def _build_opp_defense_query(opponent_abbr: str) -> str:
    """Query mv_team_defensive_ratings for a specific opponent."""
    return f"""
SELECT *
FROM mv_team_defensive_ratings
WHERE team_abbr = '{opponent_abbr}';
"""


def _build_find_picks_query() -> str:
    """Find players with high prop hit rates (>=80% over last 10)."""
    return """
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

        # Execute all in parallel
        keys = list(queries.keys())
        results = await asyncio.gather(
            *[_execute_query(pool, queries[k]) for k in keys]
        )
        for k, r in zip(keys, results):
            collected_data[k] = r

        # Add prop context
        if props:
            collected_data["requested_prop"] = props[0]

    elif intent_type == "FIND_PICKS":
        results = await _execute_query(pool, _build_find_picks_query())
        collected_data["high_hit_rate_props"] = results

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
            *[_execute_query(pool, all_queries[k]) for k in keys]
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
                *[_execute_query(pool, queries[k]) for k in keys]
            )
            for k, r in zip(keys, results):
                collected_data[k] = r
    else:
        # Fallback: treat as FIND_PICKS
        results = await _execute_query(pool, _build_find_picks_query())
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
