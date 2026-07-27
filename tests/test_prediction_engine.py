"""Unit tests for the prediction engine — pure computation, no DB or network."""

import math

import pytest

from app.services.prediction_engine import ENGINE_PARAMS, _norm_cdf, predict

STEADY = [22.0, 25.0, 19.0, 28.0, 21.0, 24.0, 26.0, 20.0, 23.0, 27.0,
          22.0, 25.0, 24.0, 21.0, 26.0, 23.0, 22.0, 25.0, 24.0, 23.0]
MINUTES = [34.0] * 20


def test_returns_a_probability():
    p = predict(STEADY, MINUTES, 20, stat="pts")
    assert p.eligible
    assert 0.0 <= p.prob <= 1.0
    assert p.sigma > 0


def test_ineligible_without_enough_games():
    p = predict(STEADY[:5], MINUTES[:5], 20, stat="pts")
    assert not p.eligible
    assert "prior games" in p.reason


def test_ineligible_on_low_minutes():
    p = predict(STEADY, [6.0] * 20, 20, stat="pts")
    assert not p.eligible


def test_probability_falls_as_the_line_rises():
    probs = [predict(STEADY, MINUTES, line, stat="pts").prob for line in (15, 20, 25, 30)]
    assert probs == sorted(probs, reverse=True), probs


def test_probability_stays_inside_the_configured_bounds():
    low = predict(STEADY, MINUTES, 2, stat="pts")
    high = predict(STEADY, MINUTES, 80, stat="pts")
    assert low.prob <= ENGINE_PARAMS["prob_cap"]
    assert high.prob >= ENGINE_PARAMS["prob_floor"]


def test_identity_calibration_is_a_no_op():
    """cal_a=0, cal_b=1 must leave the blended probability untouched."""
    p = predict(STEADY, MINUTES, 22, stat="pts", params={"cal_a": 0.0, "cal_b": 1.0})
    assert p.prob == pytest.approx(p.prob_raw, abs=1e-4)


def test_a_weaker_opponent_never_lowers_the_projection():
    tough = predict(STEADY, MINUTES, 22, stat="pts", opp_factor=0.85)
    soft = predict(STEADY, MINUTES, 22, stat="pts", opp_factor=1.15)
    assert soft.mu >= tough.mu
    assert soft.prob >= tough.prob


def test_back_to_back_reduces_the_projection():
    rested = predict(STEADY, MINUTES, 22, stat="pts", days_rest=3)
    b2b = predict(STEADY, MINUTES, 22, stat="pts", days_rest=1)
    assert b2b.mu < rested.mu


def test_minutes_trend_moves_the_projection_in_the_right_direction():
    """Inputs are most-recent-first, so the FIRST block is the current role."""
    rising = predict(STEADY, [38.0] * 10 + [24.0] * 10, 22, stat="pts")
    falling = predict(STEADY, [24.0] * 10 + [38.0] * 10, 22, stat="pts")
    flat = predict(STEADY, MINUTES, 22, stat="pts")
    assert rising.mu > flat.mu > falling.mu


def test_norm_cdf_matches_known_values():
    assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-9)
    assert _norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert _norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_half_point_and_whole_lines_differ_sensibly():
    """25+ (integer) is easier than 25.5 (must reach 26)."""
    whole = predict(STEADY, MINUTES, 25, stat="pts")
    half = predict(STEADY, MINUTES, 25.5, stat="pts")
    assert whole.prob >= half.prob
