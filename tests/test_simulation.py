"""The copula must add correlation WITHOUT disturbing each leg's marginal."""

import pytest

from app.services.simulation_service import (
    _norm_ppf, _run_monte_carlo, american_odds, decimal_to_american,
)


def _leg(name, prob, team=None, opp=None):
    return {"player_name": name, "adjusted_prob": prob, "team": team, "opponent": opp,
            "prop_type": "pts", "line": 20}


def test_marginals_are_preserved():
    legs = [_leg("A", 0.70, "BOS", "MIA"), _leg("B", 0.55, "LAL", "GSW")]
    mc = _run_monte_carlo(legs, trials=40_000, seed=3)
    for leg, simulated in zip(legs, mc["leg_probs"]):
        assert simulated == pytest.approx(leg["adjusted_prob"], abs=0.01)


def test_same_player_legs_correlate_above_independence():
    legs = [_leg("Same Guy", 0.6, "BOS", "MIA"), _leg("Same Guy", 0.6, "BOS", "MIA")]
    mc = _run_monte_carlo(legs, trials=40_000, seed=5)
    assert mc["sim_prob"] > 0.6 * 0.6 + 0.02


def test_unrelated_legs_stay_close_to_independent():
    legs = [_leg("A", 0.6, "BOS", "MIA"), _leg("B", 0.6, "LAL", "GSW")]
    mc = _run_monte_carlo(legs, trials=40_000, seed=7)
    assert mc["sim_prob"] == pytest.approx(0.36, abs=0.03)


def test_outputs_are_well_formed():
    legs = [_leg("A", 0.7), _leg("B", 0.6), _leg("C", 0.5)]
    mc = _run_monte_carlo(legs, trials=20_000, seed=11)
    assert len(mc["legs_hit_hist"]) == 4          # 0..3 legs
    assert sum(mc["legs_hit_hist"]) == pytest.approx(1.0, abs=1e-6)
    assert mc["convergence"][-1]["trials"] == 20_000
    assert all(0 <= x <= len(legs) for x in mc["sample_outcomes"])


def test_norm_ppf_inverts_the_cdf():
    assert _norm_ppf(0.5) == pytest.approx(0.0, abs=1e-6)
    assert _norm_ppf(0.975) == pytest.approx(1.96, abs=1e-3)


def test_odds_conversion():
    assert american_odds(0.5) in ("-100", "+100")
    assert american_odds(0.8).startswith("-")
    assert american_odds(0.2).startswith("+")
    assert decimal_to_american(2.0) == "+100"
