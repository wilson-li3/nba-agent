"""Pure-computation betting picks engine — no LLM calls, structured JSON output."""

import asyncio
import logging
from datetime import datetime, timezone

from app.db import get_pool
from app.services.betting_service import (
    _build_b2b_today_query,
    _build_matchup_query,
    _build_opp_defense_query,
    _build_player_team_query,
    _build_splits_query,
    _build_trend_query,
    _execute_query,
    _get_todays_schedule,
)

logger = logging.getLogger(__name__)

# ── Factor weights ──────────────────────────────────────────────────────────
FACTOR_WEIGHTS = {
    "hit_rate_l10": 0.30,
    "hit_rate_l20": 0.15,
    "trend": 0.15,
    "consistency": 0.10,
    "matchup": 0.10,
    "home_away": 0.08,
    "opp_defense": 0.07,
    "b2b": 0.05,
}

FACTOR_META = {
    "hit_rate_l10": {"label": "Hit Rate (L10)", "category": "historical"},
    "hit_rate_l20": {"label": "Hit Rate (L20)", "category": "historical"},
    "trend": {"label": "Recent Trend", "category": "trend"},
    "consistency": {"label": "Consistency", "category": "trend"},
    "matchup": {"label": "Matchup", "category": "matchup"},
    "home_away": {"label": "Home/Away", "category": "situational"},
    "opp_defense": {"label": "Opp Defense", "category": "matchup"},
    "b2b": {"label": "Rest", "category": "situational"},
}

# Prop thresholds to scan, ordered from hardest to easiest within each stat
PROP_THRESHOLDS = [
    ("pts", 30, "pts_30_hit_last10", "pts_30_hit_last20"),
    ("pts", 25, "pts_25_hit_last10", "pts_25_hit_last20"),
    ("pts", 20, "pts_20_hit_last10", "pts_20_hit_last20"),
    ("pts", 15, "pts_15_hit_last10", "pts_15_hit_last20"),
    ("reb", 10, "reb_10_hit_last10", "reb_10_hit_last20"),
    ("reb", 8, "reb_8_hit_last10", "reb_8_hit_last20"),
    ("reb", 6, "reb_6_hit_last10", "reb_6_hit_last20"),
    ("ast", 8, "ast_8_hit_last10", "ast_8_hit_last20"),
    ("ast", 6, "ast_6_hit_last10", "ast_6_hit_last20"),
    ("ast", 4, "ast_4_hit_last10", "ast_4_hit_last20"),
    ("fg3m", 4, "fg3m_4_hit_last10", "fg3m_4_hit_last20"),
    ("fg3m", 3, "fg3m_3_hit_last10", "fg3m_3_hit_last20"),
    ("fg3m", 2, "fg3m_2_hit_last10", "fg3m_2_hit_last20"),
    ("pra", 40, "pra_40_hit_last10", "pra_40_hit_last20"),
    ("pra", 30, "pra_30_hit_last10", "pra_30_hit_last20"),
]

STAT_LABELS = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA"}

# Map prop stat to the relevant opp-defense column
STAT_TO_OPP_COL = {
    "pts": "opp_ppg_allowed",
    "reb": "opp_rpg_allowed",
    "ast": "opp_apg_allowed",
    "fg3m": "opp_fg3mpg_allowed",
    "pra": "opp_ppg_allowed",  # approximate
}

# Map prop stat to trend columns
STAT_TO_TREND = {
    "pts": ("last_5_ppg", "last_15_ppg", "season_ppg"),
    "reb": ("last_5_rpg", "last_15_rpg", "season_rpg"),
    "ast": ("last_5_apg", "last_15_apg", "season_apg"),
    "fg3m": ("last_5_fg3mpg", "last_15_fg3mpg", "season_fg3mpg"),
    "pra": ("last_5_pra", "last_15_pra", "season_pra"),
}

# Map prop stat to split column
STAT_TO_SPLIT = {
    "pts": "ppg",
    "reb": "rpg",
    "ast": "apg",
    "fg3m": "fg3mpg",
    "pra": "ppg",  # approximate
}

# Map prop stat to avg/stddev columns in hit rate data
STAT_TO_AVG_STDDEV = {
    "pts": ("avg_pts_last10", "stddev_pts_last10"),
    "reb": ("avg_reb_last10", "stddev_reb_last10"),
    "ast": ("avg_ast_last10", "stddev_ast_last10"),
    "fg3m": ("avg_fg3m_last10", None),
    "pra": ("avg_pra_last10", None),
}


def _clamp(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def _f(val) -> float:
    """Convert Decimal/int/None to float."""
    if val is None:
        return 0.0
    return float(val)


def _find_best_prop(row: dict, games_l10: int) -> tuple[str, int, str, str, int, int] | None:
    """Find the highest-line prop with >=70% hit rate for a player.

    Returns (stat, line, l10_col, l20_col, hits_l10, hits_l20) or None.
    """
    if games_l10 < 6:
        return None
    for stat, line, l10_col, l20_col in PROP_THRESHOLDS:
        hits = int(row.get(l10_col, 0) or 0)
        hits_l20 = int(row.get(l20_col, 0) or 0)
        if hits >= max(int(games_l10 * 0.7), 1):
            return stat, line, l10_col, l20_col, hits, hits_l20
    return None


def _compute_factors(
    prop_stat: str,
    prop_line: int,
    hits_l10: int,
    hits_l20: int,
    games_l10: int,
    games_l20: int,
    hit_row: dict,
    trend_row: dict | None,
    split_rows: list[dict],
    matchup_row: dict | None,
    opp_def_row: dict | None,
    is_b2b: bool,
    location: str | None,
    all_opp_defense: list[dict],
) -> dict:
    """Compute all factor scores and return structured factor data."""
    factors = {}

    # ── Hit rate L10 ──
    rate_l10 = hits_l10 / max(games_l10, 1)
    factors["hit_rate_l10"] = {
        "score": round(rate_l10, 3),
        "raw": f"{hits_l10}/{games_l10}",
        "detail": f"Hit {prop_line}+ {STAT_LABELS[prop_stat]} in {hits_l10} of last {games_l10} games ({round(rate_l10 * 100)}%)",
    }

    # ── Hit rate L20 ──
    rate_l20 = hits_l20 / max(games_l20, 1)
    factors["hit_rate_l20"] = {
        "score": round(rate_l20, 3),
        "raw": f"{hits_l20}/{games_l20}",
        "detail": f"Hit {prop_line}+ {STAT_LABELS[prop_stat]} in {hits_l20} of last {games_l20} games ({round(rate_l20 * 100)}%)",
    }

    # ── Trend ──
    if trend_row:
        l5_col, l15_col, season_col = STAT_TO_TREND.get(prop_stat, ("last_5_ppg", "last_15_ppg", "season_ppg"))
        l5 = _f(trend_row.get(l5_col))
        season = _f(trend_row.get(season_col))
        if season > 0:
            trend_score = _clamp((l5 - season) / season * 2 + 0.5)
        else:
            trend_score = 0.5
        direction = "up" if l5 > season else ("down" if l5 < season else "flat")
        factors["trend"] = {
            "score": round(trend_score, 3),
            "raw": {"l5": l5, "season": season},
            "detail": f"Trending {direction}: L5 avg {l5} vs season {season}",
        }
    else:
        factors["trend"] = {"score": 0.5, "raw": None, "detail": "No trend data available"}

    # ── Consistency ──
    avg_col, std_col = STAT_TO_AVG_STDDEV.get(prop_stat, ("avg_pts_last10", "stddev_pts_last10"))
    avg_val = _f(hit_row.get(avg_col))
    std_val = _f(hit_row.get(std_col)) if std_col else 0.0
    if avg_val > 0 and std_val is not None:
        consistency = _clamp(1.0 - std_val / avg_val)
    else:
        consistency = 0.5
    factors["consistency"] = {
        "score": round(consistency, 3),
        "raw": {"avg": avg_val, "stddev": std_val},
        "detail": f"Avg {avg_val}, stddev {std_val}" + (" (very consistent)" if consistency > 0.7 else ""),
    }

    # ── Matchup ──
    if matchup_row:
        split_col_map = {"pts": "ppg", "reb": "rpg", "ast": "apg", "fg3m": "fg3mpg", "pra": "ppg"}
        matchup_avg = _f(matchup_row.get(split_col_map.get(prop_stat, "ppg")))
        if avg_val > 0:
            matchup_score = _clamp(matchup_avg / avg_val)
        else:
            matchup_score = 0.5
        games_vs = matchup_row.get("games", 0)
        factors["matchup"] = {
            "score": round(matchup_score, 3),
            "raw": {"vs_opp_avg": matchup_avg, "games": games_vs},
            "detail": f"Avg {matchup_avg} {STAT_LABELS[prop_stat]} vs this opponent ({games_vs} games)",
        }
    else:
        factors["matchup"] = {"score": 0.5, "raw": None, "detail": "No head-to-head data this season"}

    # ── Home/Away ──
    split_col = STAT_TO_SPLIT.get(prop_stat, "ppg")
    relevant_split = None
    for s in split_rows:
        loc = (s.get("location") or "").lower()
        if location and loc == location.lower():
            relevant_split = s
            break
    if relevant_split and avg_val > 0:
        split_avg = _f(relevant_split.get(split_col))
        ha_score = _clamp(split_avg / avg_val)
        loc_label = relevant_split.get("location", location or "Unknown")
        factors["home_away"] = {
            "score": round(ha_score, 3),
            "raw": {"split_avg": split_avg, "location": loc_label},
            "detail": f"{loc_label}: avg {split_avg} {STAT_LABELS[prop_stat]}",
        }
    else:
        factors["home_away"] = {"score": 0.5, "raw": None, "detail": f"{'Home' if location == 'home' else 'Away'} game"}

    # ── Opponent defense ──
    opp_col = STAT_TO_OPP_COL.get(prop_stat, "opp_ppg_allowed")
    if opp_def_row and all_opp_defense:
        opp_val = _f(opp_def_row.get(opp_col))
        all_vals = sorted([_f(r.get(opp_col)) for r in all_opp_defense])
        if all_vals:
            rank = sum(1 for v in all_vals if v <= opp_val)
            percentile = rank / len(all_vals)
        else:
            percentile = 0.5
        factors["opp_defense"] = {
            "score": round(percentile, 3),
            "raw": {"allowed": opp_val, "percentile": round(percentile, 2)},
            "detail": f"Opponent allows {opp_val} {STAT_LABELS[prop_stat]}/game (top {round(percentile * 100)}% weakest)",
        }
    else:
        factors["opp_defense"] = {"score": 0.5, "raw": None, "detail": "No opponent defense data"}

    # ── Back-to-back ──
    factors["b2b"] = {
        "score": 0.0 if is_b2b else 1.0,
        "raw": is_b2b,
        "detail": "On a back-to-back (fatigue risk)" if is_b2b else "Well rested",
    }

    return factors


def _compute_confidence(factors: dict) -> float:
    """Weighted sum of factor scores."""
    total = 0.0
    for key, weight in FACTOR_WEIGHTS.items():
        score = factors.get(key, {}).get("score", 0.5)
        total += score * weight
    return round(total, 3)


def _build_reasoning(player_name: str, prop_stat: str, prop_line: int, factors: dict, opponent: str | None, location: str | None) -> str:
    """Template-based reasoning string."""
    parts = []
    hr = factors.get("hit_rate_l10", {})
    parts.append(f"Hit {prop_line}+ {STAT_LABELS[prop_stat]} in {hr.get('raw', '?')} games ({round(hr.get('score', 0) * 100)}%).")

    trend = factors.get("trend", {})
    raw = trend.get("raw")
    if raw and isinstance(raw, dict):
        l5, season = raw.get("l5", 0), raw.get("season", 0)
        if l5 and season and l5 != season:
            parts.append(f"Averaging {l5} last 5 vs {season} season.")

    if opponent:
        opp = factors.get("opp_defense", {})
        opp_raw = opp.get("raw")
        if opp_raw and isinstance(opp_raw, dict):
            allowed = opp_raw.get("allowed")
            pct = opp_raw.get("percentile", 0.5)
            if allowed:
                parts.append(f"Faces {opponent} ({allowed} {STAT_LABELS[prop_stat]} allowed, top {round(pct * 100)}% weakest).")

    if location:
        parts.append(f"{'Home' if location == 'home' else 'Away'} game.")

    b2b = factors.get("b2b", {})
    if b2b.get("raw"):
        parts.append("Back-to-back risk.")

    return " ".join(parts)


def _build_graph_data(factors: dict) -> dict:
    """Build D3-compatible nodes and edges for a single pick."""
    nodes = [
        {
            "id": "confidence",
            "label": "Confidence",
            "value": _compute_confidence(factors),
            "weight": 1.0,
            "category": "center",
            "detail": "Composite confidence score",
        }
    ]
    for key, weight in FACTOR_WEIGHTS.items():
        f = factors.get(key, {})
        meta = FACTOR_META.get(key, {})
        nodes.append({
            "id": key,
            "label": meta.get("label", key),
            "value": f.get("score", 0.5),
            "weight": weight,
            "category": meta.get("category", "other"),
            "detail": f.get("detail", ""),
        })

    edges = []
    # Each factor connects to confidence
    for key, weight in FACTOR_WEIGHTS.items():
        edges.append({"source": key, "target": "confidence", "weight": round(weight, 2)})

    # Correlated factor pairs
    hr_l10 = factors.get("hit_rate_l10", {}).get("score", 0.5)
    hr_l20 = factors.get("hit_rate_l20", {}).get("score", 0.5)
    edges.append({"source": "hit_rate_l10", "target": "hit_rate_l20", "weight": round(0.8 if abs(hr_l10 - hr_l20) < 0.15 else 0.4, 2)})

    trend_score = factors.get("trend", {}).get("score", 0.5)
    edges.append({"source": "trend", "target": "hit_rate_l10", "weight": round(0.3 + 0.4 * trend_score, 2)})

    edges.append({"source": "matchup", "target": "opp_defense", "weight": 0.7})

    edges.append({"source": "b2b", "target": "consistency", "weight": 0.4})

    edges.append({"source": "home_away", "target": "consistency", "weight": 0.3})

    return {"nodes": nodes, "edges": edges}


async def get_structured_picks() -> dict:
    """Main entry point — returns structured picks with factor scores and graph data."""
    pool = await get_pool()

    # Step 1: Parallel fetch — high hit-rate players, schedule, B2B
    find_sql = """
    SELECT display_name, player_id,
           games_last_10, games_last_20,
           pts_15_hit_last10, pts_20_hit_last10, pts_25_hit_last10, pts_30_hit_last10,
           reb_6_hit_last10, reb_8_hit_last10, reb_10_hit_last10,
           ast_4_hit_last10, ast_6_hit_last10, ast_8_hit_last10,
           fg3m_2_hit_last10, fg3m_3_hit_last10, fg3m_4_hit_last10,
           pra_30_hit_last10, pra_40_hit_last10,
           pts_15_hit_last20, pts_20_hit_last20, pts_25_hit_last20, pts_30_hit_last20,
           reb_6_hit_last20, reb_8_hit_last20, reb_10_hit_last20,
           ast_4_hit_last20, ast_6_hit_last20, ast_8_hit_last20,
           fg3m_2_hit_last20, fg3m_3_hit_last20, fg3m_4_hit_last20,
           pra_30_hit_last20, pra_40_hit_last20,
           avg_pts_last10, stddev_pts_last10,
           avg_reb_last10, stddev_reb_last10,
           avg_ast_last10, stddev_ast_last10,
           avg_fg3m_last10, avg_pra_last10
    FROM mv_player_prop_hit_rates
    WHERE games_last_10 >= 6
      AND (
        pts_25_hit_last10 >= 7 OR pts_20_hit_last10 >= 7
        OR reb_8_hit_last10 >= 7 OR reb_6_hit_last10 >= 7
        OR ast_6_hit_last10 >= 7 OR ast_4_hit_last10 >= 7
        OR fg3m_3_hit_last10 >= 7 OR fg3m_2_hit_last10 >= 7
        OR pra_40_hit_last10 >= 7 OR pra_30_hit_last10 >= 7
      )
    ORDER BY
        GREATEST(
            pts_25_hit_last10::float / NULLIF(games_last_10, 0),
            reb_8_hit_last10::float / NULLIF(games_last_10, 0),
            ast_6_hit_last10::float / NULLIF(games_last_10, 0),
            fg3m_3_hit_last10::float / NULLIF(games_last_10, 0),
            pra_40_hit_last10::float / NULLIF(games_last_10, 0)
        ) DESC
    LIMIT 30;
    """
    b2b_sql, b2b_params = _build_b2b_today_query()

    candidates_task = asyncio.create_task(_execute_query(pool, find_sql))
    schedule_task = asyncio.create_task(_get_todays_schedule())
    b2b_task = asyncio.create_task(_execute_query(pool, b2b_sql, b2b_params))

    candidates, schedule, b2b_rows = await asyncio.gather(candidates_task, schedule_task, b2b_task)

    b2b_teams = {r["team_abbr"] for r in b2b_rows}
    games_today = len(schedule) // 2  # each game adds 2 entries

    if not candidates:
        return {
            "picks": [],
            "factor_weights": FACTOR_WEIGHTS,
            "meta": {"generated_at": datetime.now(timezone.utc).isoformat(), "games_today": games_today},
        }

    # Step 2: Resolve player teams
    player_names = [r["display_name"] for r in candidates]
    pt_sql, pt_params = _build_player_team_query(player_names)
    pt_rows = await _execute_query(pool, pt_sql, pt_params)
    player_team_map = {r["display_name"]: r["team_abbr"] for r in pt_rows}

    # Filter to players whose teams play today (if there are games)
    if schedule:
        playing_today = [r for r in candidates if player_team_map.get(r["display_name"]) in schedule]
    else:
        playing_today = candidates  # No games today — show all candidates anyway

    if not playing_today:
        playing_today = candidates[:12]  # fallback

    playing_today = playing_today[:12]

    # Step 3: Fetch all opponent defense ratings for ranking
    all_def_sql = "SELECT * FROM mv_team_defensive_ratings;"
    all_opp_defense = await _execute_query(pool, all_def_sql)

    # Step 4: Per-player data collection (parallel)
    async def _collect_player_data(row: dict) -> dict | None:
        name = row["display_name"]
        team = player_team_map.get(name)
        sched = schedule.get(team, {}) if team else {}
        opponent = sched.get("opponent")
        location = sched.get("location")
        is_b2b = team in b2b_teams if team else False

        games_l10 = int(row.get("games_last_10", 0) or 0)
        games_l20 = int(row.get("games_last_20", 0) or 0)

        best = _find_best_prop(row, games_l10)
        if not best:
            return None
        prop_stat, prop_line, l10_col, l20_col, hits_l10, hits_l20 = best

        # Parallel queries
        tasks = {}
        trend_sql, trend_params = _build_trend_query(name)
        tasks["trend"] = _execute_query(pool, trend_sql, trend_params)
        splits_sql, splits_params = _build_splits_query(name)
        tasks["splits"] = _execute_query(pool, splits_sql, splits_params)
        if opponent:
            mu_sql, mu_params = _build_matchup_query(name, opponent)
            tasks["matchup"] = _execute_query(pool, mu_sql, mu_params)
            od_sql, od_params = _build_opp_defense_query(opponent)
            tasks["opp_def"] = _execute_query(pool, od_sql, od_params)

        keys = list(tasks.keys())
        results = await asyncio.gather(*[tasks[k] for k in keys])
        data = dict(zip(keys, results))

        trend_row = data.get("trend", [None])[0] if data.get("trend") else None
        split_rows = data.get("splits", [])
        matchup_row = data.get("matchup", [None])[0] if data.get("matchup") else None
        opp_def_row = data.get("opp_def", [None])[0] if data.get("opp_def") else None

        factors = _compute_factors(
            prop_stat, prop_line, hits_l10, hits_l20,
            games_l10, games_l20, row,
            trend_row, split_rows, matchup_row, opp_def_row,
            is_b2b, location, all_opp_defense,
        )
        confidence = _compute_confidence(factors)
        reasoning = _build_reasoning(name, prop_stat, prop_line, factors, opponent, location)
        graph = _build_graph_data(factors)

        return {
            "player_name": name,
            "team": team,
            "opponent": opponent,
            "location": location,
            "prop_type": prop_stat,
            "line": prop_line,
            "confidence": confidence,
            "reasoning": reasoning,
            "is_b2b": is_b2b,
            "factors": factors,
            "graph": graph,
        }

    pick_results = await asyncio.gather(*[_collect_player_data(r) for r in playing_today])
    picks = [p for p in pick_results if p is not None]
    picks.sort(key=lambda p: p["confidence"], reverse=True)

    return {
        "picks": picks,
        "factor_weights": FACTOR_WEIGHTS,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "games_today": games_today,
        },
    }
