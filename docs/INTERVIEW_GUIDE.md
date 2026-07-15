# Interview Guide — Talking Points

How to present this project in an interview: what to lead with, the deeper
technical stories behind each component, and answers to the follow-up
questions interviewers actually ask.

---

## The 30-Second Pitch

> "I built an NBA intelligence assistant: a FastAPI backend over 1.4M rows of
> box-score data in Postgres, with an LLM router that turns natural language
> into SQL, a RAG pipeline over live news, and a betting engine that predicts
> player-prop probabilities. The prediction engine is the part I'm proudest
> of — it's a calibrated probabilistic model, not LLM guesswork, and I
> validated it with a walk-forward backtest over multiple seasons of
> historical data."

Lead with the backtest. "I measured my model's accuracy on data it had never
seen" separates you from every project that stops at "it returns
predictions."

---

## Story 1 — The Prediction Engine (the centerpiece)

### What it does
Predicts P(player clears a prop line) — e.g., "LeBron over 25.5 points" —
as a **calibrated probability**, meaning: of all the props the model prices
at 70%, roughly 70% actually hit.

### How it works (walk through these layers in order)
1. **Mean projection** — an exponentially weighted moving average
   (half-life 8 games) blended with the last-20 and season averages. Recent
   games matter more, but a single hot week doesn't swamp the baseline.
2. **Minutes trend** — if a player's minutes just jumped (new role,
   teammate injury), per-game averages lag reality. A damped minutes
   multiplier moves the forecast before the averages catch up.
3. **Context adjustments** — opponent defense (stat allowed vs league
   average, computed strictly to-date), home/away, and a back-to-back
   fatigue penalty. All *damped*: matchup effects are real but small, a few
   percent, not 30%.
4. **Variance projection** — a player's own recent standard deviation
   shrunk toward a league-wide variance model. The mean isn't enough: a
   volatile 25 PPG scorer clears 20+ less often than a steady 23 PPG scorer.
5. **Probability** — normal CDF with continuity correction gives
   P(stat ≥ line).
6. **Shrinkage blend** — the empirical last-20 hit rate is blended in with
   pseudo-count weighting, so a 9/10 streak doesn't read as "90%".
7. **Calibration layer** — a logistic recalibration (Platt scaling) fit on
   two full seasons of walk-forward predictions corrects the residual bias.

### Key phrases that land well
- "The old version scored picks with a weighted average of heuristics — the
  number *looked* like a probability but wasn't one. I rebuilt it so the
  number is falsifiable: it's a probability claim you can test."
- "Small samples regress: I blend the 10-game hit rate toward the model
  estimate with beta-style shrinkage."
- "Every adjustment is damped because in backtesting, aggressive matchup
  multipliers made calibration worse — the data says matchup effects are
  worth a few percent."

---

## Story 2 — The Backtest (the credibility layer)

### What it is
`backtest.py` replays history: for every player-game since 2021, it
generates predictions using **only data available before tip-off** (strict
walk-forward — no leakage), then scores them against the actual box scores.

### Design decisions worth mentioning
- **No lookahead leakage.** Defense ratings, league averages, home/away
  splits — everything is computed strictly to-date. Games from the same day
  are folded into the state only after the day rolls over.
- **Train/validation split by season.** Parameters and the calibration
  layer were fit on 2021-22 + 2022-23, then validated out-of-sample on
  2023-24 and 2024-25.
- **Two evaluation modes.** Fixed thresholds (20+ points) test the app's
  picks feature; synthetic "market" lines at the player's trailing median
  test the model at 50/50, where it's hardest to have an edge.
- **Metrics: Brier score, log loss, calibration table, and simulated ROI**
  at -110 juice. (See docs/BACKTEST.md for current numbers.)

### The honest answer on "would this beat Vegas?"
No — and saying so is the strong move. Sportsbook lines embed injury news,
lineup changes, and sharp money that this model doesn't see. The claim is
narrower and defensible: *the probabilities are well-calibrated against
reality*. The tuning loop (sweep parameters → refit calibration → validate
out-of-sample) is the same workflow a real quant shop uses.

---

## Story 3 — Text-to-SQL with Guardrails

- GPT-4o generates SQL from natural language against a documented schema
  with 15 generation rules (fuzzy name matching, "last N games" patterns,
  home/away splits).
- **Error recovery:** if the SQL fails, the error message goes back to the
  model for a repair attempt — a self-healing loop.
- **Safety:** queries run in read-only transactions with statement
  timeouts; mutating keywords are blocked at the application level.
  Interviewers care about the failure modes, not the happy path.

## Story 4 — RAG Pipeline

- RSS ingestion every 15 minutes → chunking → OpenAI embeddings → pgvector.
- Cosine similarity with a **recency boost** — news value decays fast, so
  pure semantic similarity isn't the right ranking.
- Answers cite sources. Grounding + citations is the standard enterprise
  RAG pattern and worth naming.

## Story 5 — Systems Design Choices

- **Materialized views** precompute hit rates, splits, and defensive
  ratings — chat answers touch pre-aggregated tables, not 1.4M-row scans.
- **asyncio.gather everywhere** — the betting pipeline fans out 6+
  independent queries in parallel.
- **The picks engine uses no LLM at all.** Deterministic, fast, testable —
  knowing when *not* to use an LLM is a talking point in 2026.
- **Router architecture** — classify → dispatch to STATS / NEWS / BETTING /
  MIXED pipelines; MIXED runs stats and news concurrently and merges.

---

## Likely Follow-Up Questions (and strong answers)

**"Why a normal distribution for points?"**
Counting stats over ~30-minute samples are approximately normal by CLT
logic; I add a continuity correction because the data is integer-valued.
For low-count stats like threes the normal is rougher — that's a known
limitation; a negative binomial would be the upgrade, and the calibration
layer absorbs most of the residual error in the meantime.

**"How do you know you're not overfitting the tuned parameters?"**
Parameters were tuned on two seasons and validated on two held-out seasons;
the calibration table stays flat out-of-sample. Also the parameter count is
tiny (~10 scalars) against ~300k backtest predictions.

**"Why does the ROI simulation look so good on threshold props?"**
Those are the model's own favorable spots at fictional -110 juice — a real
book would never price a 85% prop at -110. It demonstrates calibration, not
a get-rich strategy. The market-mode test at 50/50 lines is the honest
number.

**"What would you build next?"**
Injury/lineup awareness (biggest missing signal), possession/pace modeling,
negative binomial for count stats, per-stat calibration layers, and real
odds ingestion to measure edge against actual closing lines.

**"What was the hardest bug?"**
Lookahead leakage in the backtest — early versions updated defense ratings
with same-day games, inflating accuracy. Catching it required auditing every
piece of state the prediction touches. That story demonstrates exactly the
discipline ML interviews probe for.
