"""Monte Carlo simulation for a bet slip.

Three signals feed every simulation:

1. **Past data** — each leg is priced by the calibrated prediction engine
   (`prediction_engine.predict`), which is walk-forward validated over four
   seasons (see docs/BACKTEST.md).
2. **News** — recent articles are pulled per player via pgvector similarity
   and assessed by an LLM into a mean multiplier (injury doubt, usage bump
   from a teammate being out, minutes restriction).
3. **Correlation** — legs are simulated jointly with a Gaussian copula, so
   same-game and same-player legs move together instead of being multiplied
   as if independent.

The copula preserves each leg's calibrated marginal probability exactly:
leg *i* hits when its correlated standard-normal draw exceeds
``z*_i = Φ⁻¹(1 − p_i)``. Correlation therefore changes the *parlay*
probability without corrupting the per-leg numbers the model was validated
on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math

import numpy as np

from app.db import get_pool
from app.prompts.assess_news_impact import ASSESS_NEWS_IMPACT_PROMPT
from app.services.betting_service import _execute_query
from app.services.llm import chat_completion, embed_text
from app.services.prediction_engine import predict

logger = logging.getLogger(__name__)

TRIALS = 50_000

# Variance decomposition for the copula. Each leg's standardized draw is
#   Z_i = √w_game·Z_game + √w_player·Z_player + √(1−w_game−w_player)·Z_i
# so legs in the same game correlate at w_game, and two props on the same
# player correlate at w_game + w_player.
W_GAME = 0.10
W_PLAYER = 0.25

STAT_LABELS = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA"}
_ENGINE_STATS = set(STAT_LABELS)

# Standard prop juice: -110 per leg -> 1.909 decimal
BOOK_DECIMAL_PER_LEG = 1.0 + 100.0 / 110.0


# ── math helpers ────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
           (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def american_odds(prob: float) -> str:
    """Fair American odds for a probability."""
    if prob <= 0:
        return "+99999"
    if prob >= 1:
        return "-99999"
    if prob > 0.5:
        return f"-{round(prob / (1 - prob) * 100)}"
    return f"+{round((1 - prob) / prob * 100)}"


def decimal_to_american(dec: float) -> str:
    if dec <= 1:
        return "-99999"
    if dec >= 2:
        return f"+{round((dec - 1) * 100)}"
    return f"-{round(100 / (dec - 1))}"


# ── data loading ────────────────────────────────────────────────────────────

async def _load_player_logs(pool, player_name: str) -> list[dict]:
    sql = """
    SELECT s.min AS minutes, s.pts, s.reb, s.ast, s.fg3m, s.matchup
    FROM player_game_stats s
    WHERE s.player_id = (
        SELECT player_id FROM players
        WHERE unaccent(display_name) ILIKE unaccent('%' || $1 || '%')
        LIMIT 1
    )
      AND s.season_id = (SELECT MAX(season_id) FROM player_game_stats)
      AND s.min > 0
    ORDER BY s.game_date DESC;
    """
    return await _execute_query(pool, sql, [player_name])


async def _player_news(player_name: str, stat: str, line: float) -> dict:
    """Search recent news for this player and assess prop impact via LLM."""
    neutral = {"impact": "neutral", "multiplier": 1.0, "confidence": "low",
               "note": "No recent news found for this player.", "sources": []}
    try:
        query = f"{player_name} injury status minutes role update"
        vec = await embed_text(query)
        vec_str = "[" + ",".join(str(x) for x in vec) + "]"
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT nc.content, na.title, na.source, na.url
                FROM news_chunks nc
                JOIN news_articles na ON nc.article_id = na.article_id
                ORDER BY
                  (nc.embedding <=> $1::vector) *
                  (1.0 + GREATEST(0, EXTRACT(EPOCH FROM (NOW() - COALESCE(na.published_at, na.ingested_at)))) / 86400.0 * 0.03)
                LIMIT 4
            """, vec_str)
    except Exception:
        logger.warning("News lookup failed for %s", player_name, exc_info=True)
        return neutral

    if not rows:
        return neutral

    chunks = ""
    sources = []
    for i, r in enumerate(rows, 1):
        chunks += f"\n--- Excerpt {i} (from: {r['title']}, {r['source']}) ---\n{r['content'][:900]}\n"
        sources.append({"title": r["title"], "url": r["url"], "source": r["source"]})

    prompt = ASSESS_NEWS_IMPACT_PROMPT.format(
        player=player_name, line=line, stat=STAT_LABELS.get(stat, stat), chunks=chunks)
    try:
        raw = (await chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini", temperature=0.0, max_tokens=220,
        )).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        parsed = json.loads(raw.strip())
    except Exception:
        logger.warning("News assessment failed for %s", player_name, exc_info=True)
        return {**neutral, "sources": sources,
                "note": "Could not assess recent news; treating as neutral."}

    mult = float(parsed.get("multiplier", 1.0))
    impact = str(parsed.get("impact", "neutral")).lower()
    if impact != "out":
        mult = min(max(mult, 0.85), 1.15)
    else:
        mult = 0.0
    return {
        "impact": impact,
        "multiplier": round(mult, 3),
        "confidence": str(parsed.get("confidence", "low")).lower(),
        "note": str(parsed.get("note", ""))[:200],
        "sources": sources[:3],
    }


# ── per-leg pricing ─────────────────────────────────────────────────────────

async def _price_leg(pool, leg: dict, use_news: bool) -> dict:
    """Price one leg with the engine, then shift it by the news assessment."""
    player = (leg.get("player_name") or "").strip()
    stat = (leg.get("prop_type") or "pts").lower()
    line = float(leg.get("line") or 0)

    if stat not in _ENGINE_STATS:
        return {"player_name": player, "prop_type": stat, "line": line,
                "eligible": False, "reason": f"Unsupported prop type '{stat}'."}

    rows = await _load_player_logs(pool, player)
    if not rows:
        return {"player_name": player, "prop_type": stat, "line": line,
                "eligible": False, "reason": "No game logs found for this player."}

    def val(r):
        return float(r["pts"] + r["reb"] + r["ast"]) if stat == "pra" else float(r[stat])

    values = [val(r) for r in rows]
    minutes = [float(r["minutes"]) for r in rows]
    home_vals = [val(r) for r in rows if " vs. " in r["matchup"]]
    away_vals = [val(r) for r in rows if " vs. " not in r["matchup"]]
    ha_diff = None
    if len(home_vals) >= 5 and len(away_vals) >= 5:
        ha_diff = sum(home_vals) / len(home_vals) - sum(away_vals) / len(away_vals)

    is_home = {"home": True, "away": False}.get(leg.get("location") or "")
    p = predict(values, minutes, line, stat=stat, is_home=is_home, home_away_diff=ha_diff)
    if not p.eligible:
        return {"player_name": player, "prop_type": stat, "line": line,
                "eligible": False, "reason": p.reason}

    news = await _player_news(player, stat, line) if use_news else {
        "impact": "neutral", "multiplier": 1.0, "confidence": "low",
        "note": "News check skipped.", "sources": []}

    # Translate the news multiplier on the mean into a shift in logit space,
    # so a neutral assessment leaves the calibrated probability untouched.
    mult = news["multiplier"]
    k_win = math.ceil(line) if line != int(line) else int(line)
    threshold = k_win - 0.5
    adjusted = p.prob
    if mult <= 0.0:
        adjusted = 0.001
    elif abs(mult - 1.0) > 1e-6 and p.sigma > 0:
        base_raw = 1 - _norm_cdf((threshold - p.mu) / p.sigma)
        news_raw = 1 - _norm_cdf((threshold - p.mu * mult) / p.sigma)
        adjusted = _sigmoid(_logit(p.prob) + (_logit(news_raw) - _logit(base_raw)))
    adjusted = min(max(adjusted, 0.005), 0.995)

    return {
        "player_name": player,
        "team": leg.get("team"),
        "opponent": leg.get("opponent"),
        "prop_type": stat,
        "line": line,
        "eligible": True,
        "model_prob": round(p.prob, 4),
        "adjusted_prob": round(adjusted, 4),
        "projected_mean": p.mu,
        "projected_sd": p.sigma,
        "hit_rate_l20": p.hit_rate_l20,
        "news": news,
    }


# ── the simulation ──────────────────────────────────────────────────────────

def _game_key(leg: dict) -> str:
    """Legs in the same game share an environment factor (pace, blowout)."""
    team, opp = leg.get("team"), leg.get("opponent")
    if team and opp:
        return "|".join(sorted([str(team), str(opp)]))
    return f"solo:{leg.get('player_name')}"


def _run_monte_carlo(legs: list[dict], trials: int = TRIALS, seed: int = 7) -> dict:
    """Gaussian copula MC. Marginals stay exactly at each leg's calibrated prob."""
    rng = np.random.default_rng(seed)
    n = len(legs)

    game_ids = {}
    player_ids = {}
    g_idx, p_idx = [], []
    for leg in legs:
        gk = _game_key(leg)
        pk = str(leg.get("player_name", "")).lower()
        g_idx.append(game_ids.setdefault(gk, len(game_ids)))
        p_idx.append(player_ids.setdefault(pk, len(player_ids)))

    z_game = rng.standard_normal((trials, len(game_ids)))
    z_player = rng.standard_normal((trials, len(player_ids)))
    z_idio = rng.standard_normal((trials, n))

    w_idio = 1.0 - W_GAME - W_PLAYER
    z = (math.sqrt(W_GAME) * z_game[:, g_idx]
         + math.sqrt(W_PLAYER) * z_player[:, p_idx]
         + math.sqrt(w_idio) * z_idio)

    # Leg hits when its draw clears the threshold implied by its own probability
    thresholds = np.array([_norm_ppf(1 - leg["adjusted_prob"]) for leg in legs])
    hits = z > thresholds

    legs_hit = hits.sum(axis=1)
    all_hit = legs_hit == n

    # Running estimate at log-spaced checkpoints — this is what the UI animates
    cum = np.cumsum(all_hit)
    checkpoints = np.unique(np.geomspace(max(200, trials // 400), trials, 60).astype(int))
    convergence = [{"trials": int(t), "prob": round(float(cum[t - 1] / t), 5)}
                   for t in checkpoints]

    hist = np.bincount(legs_hit, minlength=n + 1) / trials

    return {
        "sim_prob": float(all_hit.mean()),
        "leg_probs": [float(c) for c in hits.mean(axis=0)],
        "legs_hit_hist": [round(float(x), 5) for x in hist],
        "convergence": convergence,
        "trials": trials,
    }


async def simulate_slip(legs: list[dict], use_news: bool = True) -> dict:
    """Price every leg, simulate the slip jointly, and grade the payout."""
    if not legs:
        return {"error": "Add at least one pick to the slip before simulating."}
    legs = legs[:8]

    pool = await get_pool()
    priced = await asyncio.gather(*[_price_leg(pool, leg, use_news) for leg in legs])

    usable = [p for p in priced if p.get("eligible")]
    if not usable:
        return {
            "error": "None of these legs could be priced.",
            "legs": priced,
        }

    mc = _run_monte_carlo(usable)
    for leg, sim_p in zip(usable, mc["leg_probs"]):
        leg["sim_prob"] = round(sim_p, 4)

    independent = 1.0
    for leg in usable:
        independent *= leg["adjusted_prob"]
    sim_prob = max(mc["sim_prob"], 1e-6)

    n = len(usable)
    book_decimal = BOOK_DECIMAL_PER_LEG ** n
    breakeven_decimal = 1.0 / sim_prob
    ev_per_100 = 100.0 * (sim_prob * (book_decimal - 1) - (1 - sim_prob))
    edge = sim_prob - (1.0 / book_decimal)

    model_only = 1.0
    for leg in usable:
        model_only *= leg["model_prob"]

    return {
        "legs": priced,
        "parlay": {
            "leg_count": n,
            "independent_prob": round(independent, 5),
            "sim_prob": round(sim_prob, 5),
            "correlation_effect": round(sim_prob - independent, 5),
            "news_effect": round(independent - model_only, 5),
            "fair_odds": american_odds(sim_prob),
            "breakeven_decimal": round(breakeven_decimal, 3),
            "breakeven_odds": american_odds(sim_prob),
            "book_decimal": round(book_decimal, 3),
            "book_odds": decimal_to_american(book_decimal),
            "book_implied_prob": round(1.0 / book_decimal, 5),
            "ev_per_100": round(ev_per_100, 2),
            "edge": round(edge, 5),
            "verdict": "positive" if ev_per_100 > 0 else "negative",
        },
        "legs_hit_hist": mc["legs_hit_hist"],
        "convergence": mc["convergence"],
        "trials": mc["trials"],
        "correlation": {"same_game": W_GAME, "same_player": W_GAME + W_PLAYER},
    }
