"""Probabilistic prop prediction engine.

Pure computation, no I/O — the same code path is used by the live picks
service and by backtest.py, so backtest results are honest about what the
app would have predicted.

Model
-----
1. Project the mean of the stat with a recency-weighted blend:
   exponentially weighted moving average (half-life ~8 games) blended with
   the last-20 average and season-to-date average.
2. Scale the projection by a minutes trend (recent minutes vs established
   minutes, damped) so role changes move the forecast before raw averages
   catch up.
3. Apply damped context multipliers: opponent defense (stat allowed vs
   league average), home/away, and rest (back-to-back penalty).
4. Project the standard deviation from the player's own recent variance,
   shrunk toward a league variance model (sd grows roughly with sqrt of
   the mean).
5. P(stat >= line) from a normal CDF with continuity correction.
6. Blend with the empirical last-20 hit rate (beta-style shrinkage — the
   empirical rate only gets weight in proportion to its sample size).
7. Recalibrate with a logistic map fit on historical backtests.

All tunable constants live in ENGINE_PARAMS so the backtester can sweep
them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ── Tunable parameters (fit via backtest.py; see docs/BACKTEST.md) ──────────
ENGINE_PARAMS = {
    # Mean projection blend
    "ewma_halflife": 8.0,       # games
    "w_ewma": 0.70,             # weight on EWMA
    "w_l20": 0.15,              # weight on last-20 average
    "w_season": 0.15,           # weight on season-to-date average
    # Minutes trend damping (0 = ignore minutes trend, 1 = fully scale)
    "minutes_damp": 0.50,
    # Context multiplier damping
    "opp_damp": 0.40,           # opponent defense factor damping
    "home_boost": 0.015,        # league-wide home scoring bump (fraction)
    "player_ha_damp": 0.25,     # damping on player's own home/away split
    "b2b_penalty": 0.035,       # fractional mean penalty on back-to-backs
    # Variance model: sd ≈ var_a + var_b * mu^var_pow, blended with empirical
    "var_a": 1.0,
    "var_b": 1.25,
    "var_pow": 0.62,
    "w_emp_sd": 0.15,           # weight on player's empirical sd
    "sd_floor": 1.0,
    # Empirical hit-rate blending: pseudo-count weight on the model prob
    "hit_rate_k": 70.0,
    # Calibration: p' = sigmoid(cal_a + cal_b * logit(p))
    # Fit on 2021-22 + 2022-23 walk-forward predictions (thresholds+market),
    # validated out-of-sample on 2023-24 + 2024-25 (530k predictions,
    # Brier 0.180, every calibration bucket within ±0.016). See docs/BACKTEST.md.
    "cal_a": -0.1128,
    "cal_b": 1.1427,
    # Eligibility
    "min_games": 10,
    "min_minutes": 15.0,
    "prob_cap": 0.97,
    "prob_floor": 0.03,
}

# Per-stat variance overrides — low-count stats (threes) are not normal
# enough at the default parameters.
STAT_VAR = {
    "pts": {"var_a": 1.2, "var_b": 1.30, "var_pow": 0.62},
    "reb": {"var_a": 0.9, "var_b": 1.10, "var_pow": 0.60},
    "ast": {"var_a": 0.8, "var_b": 1.05, "var_pow": 0.60},
    "fg3m": {"var_a": 0.7, "var_b": 0.75, "var_pow": 0.55},
    "pra": {"var_a": 1.6, "var_b": 1.35, "var_pow": 0.62},
}


@dataclass
class Prediction:
    """A single prop prediction with full factor attribution."""

    prob: float                 # calibrated P(stat >= line)
    prob_raw: float             # pre-calibration blended probability
    prob_model: float           # distribution-only probability
    hit_rate_l20: float         # empirical rate used in the blend
    mu: float                   # projected mean
    sigma: float                # projected sd
    eligible: bool = True
    reason: str = ""
    factors: dict = field(default_factory=dict)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _ewma(values: list[float], halflife: float) -> float:
    """EWMA over values ordered most-recent-first."""
    if not values:
        return 0.0
    alpha = 1.0 - 0.5 ** (1.0 / halflife)
    num = 0.0
    den = 0.0
    w = 1.0
    for v in values:
        num += w * v
        den += w
        w *= 1.0 - alpha
    return num / den if den else 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def predict(
    stat_values: list[float],
    minutes: list[float],
    line: float,
    *,
    stat: str = "pts",
    opp_factor: float = 1.0,
    is_home: bool | None = None,
    home_away_diff: float | None = None,
    days_rest: int | None = None,
    params: dict | None = None,
) -> Prediction:
    """Predict P(stat >= line) for one player-game.

    Args:
        stat_values: the player's prior values for this stat, most recent
            FIRST, season-to-date (never includes the game being predicted).
        minutes: minutes played in those same games, same order.
        line: the prop line; probability computed as P(value >= line).
        stat: one of pts/reb/ast/fg3m/pra (variance model selection).
        opp_factor: opponent's stat allowed per game divided by the league
            average allowed, computed strictly to-date. 1.0 = average.
        is_home: True/False if known.
        home_away_diff: player's own (home mean - away mean) for this stat,
            to-date; damped before use. None = league default only.
        days_rest: days since the player's team last played. 1 = back-to-back.
        params: optional override of ENGINE_PARAMS (used by tuning sweeps).
    """
    P = {**ENGINE_PARAMS, **(params or {})}

    n = len(stat_values)
    if n < P["min_games"]:
        return Prediction(0.5, 0.5, 0.5, 0.0, 0.0, 0.0, eligible=False,
                          reason=f"only {n} prior games (need {P['min_games']})")

    recent_min = _ewma(minutes[:10], 5.0) if minutes else 0.0
    if recent_min < P["min_minutes"]:
        return Prediction(0.5, 0.5, 0.5, 0.0, 0.0, 0.0, eligible=False,
                          reason=f"projected minutes {recent_min:.0f} below {P['min_minutes']:.0f}")

    # ── 1. Base mean: recency-weighted blend ──
    ewma_val = _ewma(stat_values, P["ewma_halflife"])
    l20_avg = _mean(stat_values[:20])
    season_avg = _mean(stat_values)
    mu = P["w_ewma"] * ewma_val + P["w_l20"] * l20_avg + P["w_season"] * season_avg

    # ── 2. Minutes trend ──
    minutes_ratio = 1.0
    if minutes and len(minutes) >= 10:
        m_recent = _ewma(minutes[:5], 3.0)
        m_baseline = _mean(minutes[:20])
        if m_baseline > 0:
            raw_ratio = m_recent / m_baseline
            minutes_ratio = 1.0 + P["minutes_damp"] * (raw_ratio - 1.0)
            minutes_ratio = min(max(minutes_ratio, 0.75), 1.25)
    mu *= minutes_ratio

    # ── 3. Context multipliers ──
    opp_mult = 1.0 + P["opp_damp"] * (opp_factor - 1.0)
    opp_mult = min(max(opp_mult, 0.85), 1.15)
    mu *= opp_mult

    ha_mult = 1.0
    if is_home is not None:
        ha_mult = 1.0 + (P["home_boost"] if is_home else -P["home_boost"])
        if home_away_diff is not None and season_avg > 0:
            signed = home_away_diff / 2.0 if is_home else -home_away_diff / 2.0
            ha_mult *= 1.0 + P["player_ha_damp"] * (signed / season_avg)
        ha_mult = min(max(ha_mult, 0.92), 1.08)
    mu *= ha_mult

    rest_mult = 1.0
    if days_rest is not None and days_rest <= 1:
        rest_mult = 1.0 - P["b2b_penalty"]
    mu *= rest_mult

    # ── 4. Variance projection ──
    v = STAT_VAR.get(stat, {})
    var_a = v.get("var_a", P["var_a"])
    var_b = v.get("var_b", P["var_b"])
    var_pow = v.get("var_pow", P["var_pow"])
    model_sd = var_a + var_b * (max(mu, 0.1) ** var_pow)
    emp_sd = _std(stat_values[:20])
    if emp_sd > 0:
        sigma = P["w_emp_sd"] * emp_sd + (1 - P["w_emp_sd"]) * model_sd
    else:
        sigma = model_sd
    sigma = max(sigma, P["sd_floor"])

    # ── 5. Distribution probability (continuity-corrected normal) ──
    # P(X >= k) for integer-valued X: Φ((k - 0.5 - mu) / sigma), where the
    # smallest winning integer k is line itself for whole-number lines
    # ("25+ pts") and ceil(line) for book-style half-point lines (25.5).
    k_win = math.ceil(line) if line != int(line) else int(line)
    z = (k_win - 0.5 - mu) / sigma
    prob_model = 1.0 - _norm_cdf(z)

    # ── 6. Blend with empirical hit rate (shrinkage) ──
    window = stat_values[:20]
    hits = sum(1 for x in window if x >= line)
    n_win = len(window)
    hit_rate = hits / n_win if n_win else 0.0
    k = P["hit_rate_k"]
    prob_blend = (n_win * hit_rate + k * prob_model) / (n_win + k)

    # ── 7. Calibration ──
    prob_cal = _sigmoid(P["cal_a"] + P["cal_b"] * _logit(prob_blend))
    prob_cal = min(max(prob_cal, P["prob_floor"]), P["prob_cap"])

    factors = {
        "recent_form": {
            "score": round(min(max(ewma_val / max(season_avg, 0.1), 0.0), 2.0) / 2.0, 3),
            "raw": {"ewma": round(ewma_val, 2), "season": round(season_avg, 2)},
            "detail": f"Recency-weighted avg {ewma_val:.1f} vs season {season_avg:.1f}",
        },
        "hit_rate_l20": {
            "score": round(hit_rate, 3),
            "raw": f"{hits}/{n_win}",
            "detail": f"Cleared {line:g}+ in {hits} of last {n_win} games",
        },
        "minutes_trend": {
            "score": round(min(max((minutes_ratio - 0.75) / 0.5, 0.0), 1.0), 3),
            "raw": round(minutes_ratio, 3),
            "detail": f"Minutes trend multiplier {minutes_ratio:.2f}",
        },
        "opp_defense": {
            "score": round(min(max((opp_factor - 0.85) / 0.3, 0.0), 1.0), 3),
            "raw": round(opp_factor, 3),
            "detail": f"Opponent allows {opp_factor:.0%} of league average",
        },
        "home_away": {
            "score": round(min(max((ha_mult - 0.92) / 0.16, 0.0), 1.0), 3),
            "raw": round(ha_mult, 3),
            "detail": ("Home" if is_home else "Away") + f" adjustment {ha_mult:.2f}" if is_home is not None else "Unknown venue",
        },
        "rest": {
            "score": 0.0 if rest_mult < 1.0 else 1.0,
            "raw": days_rest,
            "detail": "Back-to-back (fatigue penalty applied)" if rest_mult < 1.0 else "Rested",
        },
        "volatility": {
            "score": round(min(max(1.0 - sigma / max(mu, 1.0), 0.0), 1.0), 3),
            "raw": {"sigma": round(sigma, 2), "mu": round(mu, 2)},
            "detail": f"Projected {mu:.1f} ± {sigma:.1f}",
        },
    }

    return Prediction(
        prob=round(prob_cal, 4),
        prob_raw=round(prob_blend, 4),
        prob_model=round(prob_model, 4),
        hit_rate_l20=round(hit_rate, 4),
        mu=round(mu, 2),
        sigma=round(sigma, 2),
        factors=factors,
    )
