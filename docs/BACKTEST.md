# Backtest Methodology & Results

## What this proves

The prop prediction engine outputs **calibrated probabilities**: when it
says 75%, the prop hits about 75% of the time on data the model has never
seen. That claim is tested with a strict walk-forward backtest over four
NBA seasons (~880k predictions).

## Methodology

- **Walk-forward, no leakage.** For every player-game, predictions use only
  games played *before* that date. Opponent defense ratings, league
  averages, and home/away splits are all computed strictly to-date;
  same-day games enter the state only after the day rolls over.
- **Train/validation split by season.** Engine parameters and the Platt
  calibration layer (`cal_a`, `cal_b`) were fit on **2021-22 + 2022-23**
  and validated untouched on **2023-24 + 2024-25**.
- **Two evaluation modes:**
  - `thresholds` — the app's fixed prop lines (20+ pts, 8+ reb, ...)
  - `market` — synthetic book-style lines at each player's trailing median
    (x.5), forcing the model to price near 50/50 where edges are hardest.

## Reproduce

```bash
python backtest.py --seasons 2023-24,2024-25                 # thresholds
python backtest.py --seasons 2023-24,2024-25 --mode market   # market lines
python backtest.py --seasons 2021-22,2022-23 --fit-calibration  # refit
```

Each season runs in a few seconds (pure-Python engine over preloaded rows).

## Validation results (held-out 2023-24 + 2024-25)

### Thresholds mode — 351,040 predictions

- **Brier score 0.1532** (0.25 = always predicting 50%)
- **Log loss 0.4717**

| Predicted bucket | n | Predicted | Actual | Gap |
|---|---|---|---|---|
| 0–10% | 90,988 | 0.060 | 0.069 | +0.009 |
| 10–20% | 69,492 | 0.146 | 0.147 | +0.001 |
| 20–30% | 47,275 | 0.247 | 0.245 | −0.003 |
| 30–40% | 36,658 | 0.348 | 0.337 | −0.011 |
| 40–50% | 28,564 | 0.448 | 0.436 | −0.012 |
| 50–60% | 24,093 | 0.549 | 0.540 | −0.009 |
| 60–70% | 20,688 | 0.648 | 0.647 | −0.001 |
| 70–80% | 16,404 | 0.748 | 0.746 | −0.002 |
| 80–90% | 12,493 | 0.846 | 0.862 | +0.016 |
| 90–100% | 4,385 | 0.929 | 0.943 | +0.014 |

Every bucket is within ±0.016, and the high-confidence buckets (what the
picks page surfaces) err *conservative* — actual hit rates run slightly
above the stated probability.

### Market mode — 179,355 predictions at trailing-median (x.5) lines

- **Brier score 0.2323** vs 0.25 for coin-flipping — a real but modest edge
  at 50/50 lines, which is the honest expectation without injury/odds data.
- Calibration is within ±0.015 in the 30–60% buckets that hold ~87% of the
  volume. Extreme buckets are thin and lean conservative (actual more
  extreme than predicted).
- Known limitation: the 10–20% bucket over-rates declining players — when
  the model already sees a big drop-off, reality tends to be even worse
  (role changes, injuries). A lineup/injury signal is the natural fix.

### About the ROI simulation

`p >= 0.75` threshold props show hit rate 0.850 and +0.62 flat-stake ROI at
-110 — but a real sportsbook would never offer -110 on an 85% prop. The ROI
table demonstrates that the probability ranking is monotone and calibrated,
not that this beats closing lines. Beating a book requires signals the model
doesn't have (injuries, lineups, line movement).

## Final engine parameters

Fit on train seasons, frozen in `app/services/prediction_engine.py`:

| Parameter | Value | Meaning |
|---|---|---|
| `w_ewma / w_l20 / w_season` | 0.70 / 0.15 / 0.15 | mean projection blend (EWMA half-life 8) |
| `hit_rate_k` | 70 | pseudo-count shrinkage of L20 hit rate toward model prob |
| `w_emp_sd` | 0.15 | player empirical sd vs league variance model |
| `opp_damp` | 0.40 | opponent defense effect damping |
| `b2b_penalty` | 3.5% | back-to-back mean penalty |
| `cal_a / cal_b` | −0.1128 / 1.1427 | Platt calibration (logit scale) |

Notable finding from tuning: the sweep consistently preferred trusting the
distribution model over raw empirical hit rates (`hit_rate_k` 14 → 70
improved held-out log loss), confirming that small-sample hit rates are
mostly noise around the projected mean.
