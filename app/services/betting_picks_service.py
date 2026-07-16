"""Structured betting picks powered by the probabilistic prediction engine.

The engine (app/services/prediction_engine.py) is pure computation and is
the exact code path validated by backtest.py — the confidence shown in the
UI is a calibrated probability, not a heuristic score.
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timezone

from app.db import get_pool
from app.services.betting_service import _execute_query, _get_todays_schedule
from app.services.prediction_engine import ENGINE_PARAMS, Prediction, predict

logger = logging.getLogger(__name__)

# Display weights for the factor sliders (UI what-if tool). The engine's
# real math is not a weighted sum — these describe relative influence.
FACTOR_WEIGHTS = {
    "recent_form": 0.25,
    "hit_rate_l20": 0.20,
    "volatility": 0.15,
    "minutes_trend": 0.12,
    "opp_defense": 0.10,
    "rest": 0.10,
    "home_away": 0.08,
}

FACTOR_META = {
    "recent_form": {"label": "Recent Form", "category": "trend"},
    "hit_rate_l20": {"label": "Hit Rate (L20)", "category": "historical"},
    "volatility": {"label": "Volatility", "category": "derived"},
    "minutes_trend": {"label": "Minutes Trend", "category": "advanced"},
    "opp_defense": {"label": "Opp Defense", "category": "matchup"},
    "rest": {"label": "Rest", "category": "situational"},
    "home_away": {"label": "Home/Away", "category": "situational"},
}

# Lines scanned per stat, hardest first
PROP_THRESHOLDS = {
    "pts": [30, 25, 20, 15],
    "reb": [10, 8, 6],
    "ast": [8, 6, 4],
    "fg3m": [4, 3, 2],
    "pra": [40, 30],
}

STAT_LABELS = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA"}

MIN_PICK_PROB = 0.70   # a prop must clear this to make the board
MAX_PICKS = 12

# In-memory response cache — the underlying data only changes when new box
# scores land, so recomputing picks on every page load is wasted work.
_CACHE_TTL_SECONDS = 300
_cache: dict = {"payload": None, "expires_at": 0.0}


async def _load_season_logs(pool) -> list[dict]:
    """All game logs for the latest season, most recent first per player."""
    sql = """
    SELECT s.player_id, p.display_name, p.is_active, s.team_abbr, s.game_date,
           s.matchup, s.min AS minutes, s.pts, s.reb, s.ast, s.fg3m
    FROM player_game_stats s
    JOIN players p USING (player_id)
    WHERE s.season_id = (SELECT MAX(season_id) FROM player_game_stats)
      AND s.min > 0
    ORDER BY s.player_id, s.game_date DESC;
    """
    return await _execute_query(pool, sql)


def _build_histories(rows: list[dict]) -> dict:
    """Group logs into per-player history dicts (rows already newest-first)."""
    players: dict = {}
    for r in rows:
        pid = r["player_id"]
        h = players.get(pid)
        if h is None:
            h = players[pid] = {
                "name": r["display_name"],
                "is_active": r["is_active"],
                "team": r["team_abbr"],       # team of most recent game
                "last_game": r["game_date"],
                "stats": {s: [] for s in ("pts", "reb", "ast", "fg3m", "pra")},
                "minutes": [],
                "ha": defaultdict(list),
            }
        pra = r["pts"] + r["reb"] + r["ast"]
        is_home = " vs. " in r["matchup"]
        for stat, val in (("pts", r["pts"]), ("reb", r["reb"]), ("ast", r["ast"]),
                          ("fg3m", r["fg3m"]), ("pra", pra)):
            h["stats"][stat].append(float(val))
            h["ha"][f"{stat}_{'home' if is_home else 'away'}"].append(float(val))
        h["minutes"].append(float(r["minutes"]))
    return players


def _build_defense_factors(rows: list[dict]) -> dict:
    """{(team, stat): allowed_per_game / league_avg} from season game logs."""
    totals: dict = defaultdict(lambda: defaultdict(float))
    game_teams: dict = defaultdict(set)
    for r in rows:
        gkey = (r["game_date"], frozenset(_matchup_teams(r["matchup"])))
        totals[(gkey, r["team_abbr"])]["pts"] += r["pts"]
        totals[(gkey, r["team_abbr"])]["reb"] += r["reb"]
        totals[(gkey, r["team_abbr"])]["ast"] += r["ast"]
        totals[(gkey, r["team_abbr"])]["fg3m"] += r["fg3m"]
        game_teams[gkey].add(r["team_abbr"])

    allowed_sum: dict = defaultdict(lambda: defaultdict(float))
    allowed_n: dict = defaultdict(int)
    league_sum: dict = defaultdict(float)
    league_n = 0
    for gkey, teams in game_teams.items():
        if len(teams) != 2:
            continue
        a, b = tuple(teams)
        for team, opp in ((a, b), (b, a)):
            for s in ("pts", "reb", "ast", "fg3m"):
                allowed_sum[team][s] += totals[(gkey, opp)][s]
                league_sum[s] += totals[(gkey, opp)][s]
            allowed_n[team] += 1
            league_n += 1

    factors: dict = {}
    for team, n in allowed_n.items():
        if n < 5 or league_n < 50:
            continue
        for s in ("pts", "reb", "ast", "fg3m"):
            league_avg = league_sum[s] / league_n
            if league_avg > 0:
                factors[(team, s)] = (allowed_sum[team][s] / n) / league_avg
    return factors


def _matchup_teams(matchup: str) -> tuple[str, str]:
    """'LAL @ BOS' or 'LAL vs. BOS' -> ('LAL', 'BOS')."""
    sep = " @ " if " @ " in matchup else " vs. "
    left, right = matchup.split(sep, 1)
    return left.strip(), right.strip()


def _opp_factor(defense: dict, opponent: str | None, stat: str) -> float:
    if not opponent:
        return 1.0
    s = "pts" if stat == "pra" else stat
    return defense.get((opponent, s), 1.0)


def _ha_diff(h: dict, stat: str) -> float | None:
    home = h["ha"].get(f"{stat}_home", [])
    away = h["ha"].get(f"{stat}_away", [])
    if len(home) >= 5 and len(away) >= 5:
        return sum(home) / len(home) - sum(away) / len(away)
    return None


def _best_prop(h: dict, defense: dict, opponent: str | None,
               is_home: bool | None, days_rest: int | None) -> tuple | None:
    """Highest-line prop with calibrated prob >= MIN_PICK_PROB; best across stats."""
    best: tuple | None = None
    for stat, lines in PROP_THRESHOLDS.items():
        values = h["stats"][stat]
        opp_f = _opp_factor(defense, opponent, stat)
        ha = _ha_diff(h, stat)
        for line in lines:  # hardest first
            p = predict(values, h["minutes"], line, stat=stat, opp_factor=opp_f,
                        is_home=is_home, home_away_diff=ha, days_rest=days_rest)
            if not p.eligible:
                break  # same for every line of this stat
            if p.prob >= MIN_PICK_PROB:
                if best is None or p.prob > best[2].prob:
                    best = (stat, line, p)
                break  # take the hardest qualifying line per stat
    return best


def _build_reasoning(name: str, stat: str, line: int, p: Prediction,
                     opponent: str | None, location: str | None, is_b2b: bool) -> str:
    parts = [
        f"Model projects {p.mu:.1f} {STAT_LABELS[stat]} (±{p.sigma:.1f}) vs the {line}+ line — "
        f"a {p.prob:.0%} calibrated probability."
    ]
    hr = p.factors.get("hit_rate_l20", {}).get("raw")
    if hr:
        parts.append(f"Cleared it in {hr} recent games.")
    if opponent:
        of = p.factors.get("opp_defense", {}).get("raw")
        if of and abs(of - 1.0) > 0.03:
            parts.append(f"{opponent} allows {of:.0%} of league average — "
                         f"{'a favorable' if of > 1 else 'a tough'} matchup.")
    if location:
        parts.append(f"{'Home' if location == 'home' else 'Road'} game.")
    if is_b2b:
        parts.append("Back-to-back: fatigue penalty applied.")
    return " ".join(parts)


def _build_graph_data(p: Prediction, confidence: float) -> dict:
    """D3 nodes/edges — every node is a real quantity from the model."""
    nodes = [{
        "id": "confidence",
        "label": "Probability",
        "value": confidence,
        "weight": 1.0,
        "category": "center",
        "detail": f"Calibrated probability the prop hits: {confidence:.0%}",
    }]
    for key, weight in FACTOR_WEIGHTS.items():
        f = p.factors.get(key, {})
        meta = FACTOR_META.get(key, {})
        nodes.append({
            "id": key,
            "label": meta.get("label", key),
            "value": f.get("score", 0.5),
            "weight": weight,
            "category": meta.get("category", "other"),
            "detail": f.get("detail", ""),
        })

    # Real derived nodes from the model internals
    nodes.append({
        "id": "projection",
        "label": "Projection",
        "value": p.prob_model,
        "weight": 0.18,
        "category": "derived",
        "detail": f"Distribution model: projected {p.mu:.1f} ± {p.sigma:.1f} → {p.prob_model:.0%} over",
    })
    nodes.append({
        "id": "calibration",
        "label": "Calibration",
        "value": p.prob,
        "weight": 0.10,
        "category": "meta",
        "detail": f"Raw blend {p.prob_raw:.0%} → calibrated {p.prob:.0%} (fit on 300k+ backtested props)",
    })

    edges = [{"source": k, "target": "confidence", "weight": round(w, 2)}
             for k, w in FACTOR_WEIGHTS.items()]
    edges += [
        {"source": "recent_form", "target": "projection", "weight": 0.55},
        {"source": "minutes_trend", "target": "projection", "weight": 0.35},
        {"source": "volatility", "target": "projection", "weight": 0.45},
        {"source": "opp_defense", "target": "projection", "weight": 0.25},
        {"source": "home_away", "target": "projection", "weight": 0.15},
        {"source": "rest", "target": "projection", "weight": 0.15},
        {"source": "projection", "target": "calibration", "weight": 0.50},
        {"source": "hit_rate_l20", "target": "calibration", "weight": 0.40},
        {"source": "calibration", "target": "confidence", "weight": 0.60},
        {"source": "projection", "target": "confidence", "weight": 0.45},
    ]
    return {"nodes": nodes, "edges": edges}


async def get_structured_picks() -> dict:
    """Main entry point — calibrated picks with factor attribution."""
    if _cache["payload"] is not None and time.monotonic() < _cache["expires_at"]:
        return _cache["payload"]

    pool = await get_pool()

    logs_task = asyncio.create_task(_load_season_logs(pool))
    schedule_task = asyncio.create_task(_get_todays_schedule())
    rows, schedule = await asyncio.gather(logs_task, schedule_task)

    games_today = len(schedule) // 2
    if not rows:
        return {"picks": [], "factor_weights": FACTOR_WEIGHTS,
                "meta": {"generated_at": datetime.now(timezone.utc).isoformat(),
                         "games_today": games_today}}

    players = _build_histories(rows)
    defense = _build_defense_factors(rows)
    today = date.today()

    picks = []
    for pid, h in players.items():
        if not h["is_active"]:
            continue
        team = h["team"]
        sched = schedule.get(team, {})
        opponent = sched.get("opponent")
        location = sched.get("location")
        is_home = {"home": True, "away": False}.get(location)

        days_rest = (today - h["last_game"]).days if h["last_game"] else None
        # Only meaningful in-season; ignore stale gaps (offseason demo mode)
        if days_rest is not None and days_rest > 30:
            days_rest = None
        is_b2b = days_rest == 1

        # If there are games today, only pick players who play
        if schedule and team not in schedule:
            continue

        best = _best_prop(h, defense, opponent, is_home, days_rest)
        if not best:
            continue
        stat, line, p = best
        confidence = p.prob
        picks.append({
            "player_name": h["name"],
            "team": team,
            "opponent": opponent,
            "location": location,
            "prop_type": stat,
            "line": line,
            "confidence": confidence,
            "probability_raw": p.prob_raw,
            "projected_mean": p.mu,
            "projected_sd": p.sigma,
            "reasoning": _build_reasoning(h["name"], stat, line, p, opponent, location, is_b2b),
            "is_b2b": is_b2b,
            "factors": p.factors,
            "graph": _build_graph_data(p, confidence),
        })

    picks.sort(key=lambda x: x["confidence"], reverse=True)
    picks = picks[:MAX_PICKS]

    payload = {
        "picks": picks,
        "factor_weights": FACTOR_WEIGHTS,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "games_today": games_today,
            "model": "probabilistic-v2",
            "engine": {
                "min_games": ENGINE_PARAMS["min_games"],
                "calibration": [ENGINE_PARAMS["cal_a"], ENGINE_PARAMS["cal_b"]],
            },
        },
    }
    _cache["payload"] = payload
    _cache["expires_at"] = time.monotonic() + _CACHE_TTL_SECONDS
    return payload
