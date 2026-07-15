# Ball Don't Lie — NBA Intelligence Assistant

## What Is This?

An AI-powered NBA assistant that lets you ask natural language questions about basketball stats, news, and betting — and get answers backed by real data. It combines a text-to-SQL pipeline, RAG-based news search, a multi-factor betting engine, and live score tracking into a single conversational interface.

**Stack:** Python/FastAPI, PostgreSQL + pgvector, OpenAI GPT-4o, D3.js, vanilla JS

---

## How It Works (High-Level Pipeline)

```
User Question
    │
    ├── resolve_context()     ← rewrites follow-ups using chat history
    ├── normalize_question()  ← resolves nicknames (Steph → Stephen Curry), slang (boards → rebounds)
    ├── classify_question()   ← routes to one of 5 categories:
    │
    ├─ STATS   → Text-to-SQL → execute query → format results in natural language
    ├─ NEWS    → Embed question → pgvector similarity search → summarize with citations
    ├─ MIXED   → run STATS + NEWS in parallel → combine answers
    ├─ BETTING → parse intent → 6+ parallel DB queries + calibrated engine probability → analyst-grade synthesis
    └─ OFF_TOPIC → polite rejection
```

---

## Notable Files for Demo

### Core Pipeline (the "brain")

| File | What It Does |
|------|-------------|
| `app/services/router_service.py` | **The orchestrator.** Classifies questions, resolves context from chat history, dispatches to the right service. Start here to understand the full flow. |
| `app/services/stats_service.py` | **Text-to-SQL pipeline.** Takes a natural language question, generates SQL via GPT-4o, executes it against PostgreSQL, and formats the result. Has error recovery — if the SQL fails, it asks the LLM to fix it. |
| `app/services/news_service.py` | **RAG pipeline.** Embeds the question, runs pgvector similarity search on news chunks, returns summarized answer with source citations. |
| `app/services/betting_service.py` | **Conversational betting analysis.** Parses betting intent (prop check, find picks, parlay, game preview), builds and runs 6+ parallel queries, synthesizes with GPT-4o. |
| `app/services/betting_picks_service.py` | **Structured picks service (no LLM).** Runs the prediction engine over every eligible player, returns calibrated picks with factor attribution and D3 graph data. |
| `app/services/prediction_engine.py` | **The prediction engine.** Pure-computation probabilistic model: EWMA mean projection, variance model, context adjustments, shrinkage, Platt calibration. Same code path in the live API and the backtest. |
| `backtest.py` | **Walk-forward backtester.** Replays four seasons with zero lookahead leakage; reports Brier score, log loss, calibration tables, simulated ROI. |

### Prompts (LLM instructions)

| File | What It Does |
|------|-------------|
| `app/prompts/text_to_sql.py` | Full database schema + 15 rules for SQL generation. Handles fuzzy name matching, "last N games" patterns, home/away splits, opponent matchups. |
| `app/prompts/classify.py` | 5-category classifier with decision rules and examples. |
| `app/prompts/format_betting.py` | Betting output templates — STRONG LEAN (≥80% hit rate), LEAN (≥70%), COIN FLIP. |
| `app/prompts/parse_betting.py` | Extracts structured intent from betting questions (players, props, teams, type). |

### Data Ingestion

| File | What It Does |
|------|-------------|
| `sync_games.py` | Loads all NBA games (1946–present) from NBA API into `games` table. |
| `sync_players.py` | Loads all players + bios (height, weight, draft info, school). |
| `sync_player_stats.py` | Bulk-loads box scores from PlayerGameLogs — one API call per season. |
| `sync_news.py` | Fetches RSS feeds (ESPN, CBS, Yahoo, etc.), chunks articles, embeds with OpenAI, stores in pgvector. Runs every 15 minutes automatically. |
| `refresh_aggregates.py` | Creates 7 materialized views for fast pre-computed stats (hit rates, splits, defense ratings, etc.). |

### Frontend

| File | What It Does |
|------|-------------|
| `frontend/index.html` | Main chat interface — hero input, live scores ticker, headlines ticker, message rendering with markdown + citations. |
| `frontend/betting.html` | 3-column betting dashboard — picks list, D3 force graph, bet slip with parlay builder. |

---

## Database Architecture

**Core tables:** `games`, `teams`, `players`, `player_game_stats`, `news_articles`, `news_chunks`

**Materialized views** (pre-computed for speed):
- `mv_player_prop_hit_rates` — how often a player hits thresholds (20+ pts, 8+ reb, etc.) over last 10 and 20 games
- `mv_player_season_averages` — PPG, RPG, APG, shooting %
- `mv_player_home_away_splits` — performance at home vs away
- `mv_team_defensive_ratings` — points/rebounds/assists allowed per team
- `mv_team_back_to_backs` — flags games on zero rest
- `mv_player_career_totals` — career stats + highs
- `mv_player_milestone_games` — 40-point games, triple-doubles, etc.

---

## The Prediction Engine — How It Works

### Overview

The betting picks page (`/betting/picks`) is powered by a **calibrated probabilistic prediction engine** (`app/services/prediction_engine.py`) — pure computation, no LLM calls. The confidence number on every pick is a real probability claim: *of all props the engine prices at 75%, about 75% actually hit* — verified by a walk-forward backtest over four seasons (see `docs/BACKTEST.md`).

### The Model (7 layers)

1. **Mean projection** — EWMA of the stat (half-life 8 games) blended 70/15/15 with the last-20 and season averages.
2. **Minutes trend** — a damped multiplier from recent vs established minutes, so role changes move the forecast before averages catch up.
3. **Context adjustments** — opponent defense (stat allowed vs league average, to-date), home/away, back-to-back penalty. All damped: matchup effects are worth a few percent, not 30%.
4. **Variance projection** — the player's recent standard deviation shrunk toward a league variance model (`sd ≈ a + b·μ^p`, per stat).
5. **Probability** — continuity-corrected normal CDF gives P(stat ≥ line).
6. **Shrinkage blend** — the empirical L20 hit rate is blended in with pseudo-count weighting (k=70), so a hot 9/10 streak doesn't read as "90%".
7. **Platt calibration** — a logistic recalibration fit on 2021-22 + 2022-23 walk-forward predictions corrects residual bias.

### Validation (held-out 2023-24 + 2024-25, 530k predictions)

- **Brier score 0.153** on threshold props (0.25 = coin-flipping)
- Every calibration bucket within **±0.016** of its predicted rate
- High-confidence buckets err conservative (actual ≥ predicted)
- Reproduce: `python backtest.py --seasons 2023-24,2024-25`

### Filtering

Only shows picks where the player's team plays today (when there are games), the player has 10+ games and ~15+ projected minutes, and the calibrated probability ≥ 70%. Top 12 by probability.

---

## The D3 Force Graph — How It Works

### What You're Looking At

The graph is an **interactive force-directed network** that visualizes how each factor contributes to a pick's confidence score. Each node is a factor, and the central node is the final confidence.

### Node Types

**Every node is a real quantity from the model** (no decorative filler):
- 1 **Probability node** (center, large, orange) — the calibrated probability
- 7 **Factor nodes** — Recent Form, Hit Rate L20, Volatility, Minutes Trend, Opp Defense, Rest, Home/Away
- 2 **Derived nodes** — Projection (the distribution model's μ ± σ vs the line) and Calibration (raw blend → calibrated probability)

### Visual Encoding

| Visual Property | What It Represents |
|----------------|-------------------|
| **Node size** | Weight of the factor (higher weight = bigger circle) |
| **Node opacity** | Score of the factor (higher score = more opaque) |
| **Node color** | Category — orange=historical, peach=trend, green=matchup, gray=situational, blue=derived, purple=context, teal=advanced, rose=meta |
| **Edge thickness** | Strength of connection between factors |
| **Edge opacity** | Same — stronger connections are more visible |

### The Physics

D3's force simulation applies 4 forces:

1. **Link force** — pulls connected nodes together (stronger connection = stronger pull)
2. **Charge force** — all nodes repel each other (prevents overlap), scales with container size
3. **Center force** — pulls everything toward the center of the SVG
4. **Collision force** — prevents nodes from overlapping (radius + 16px padding)

The simulation scales to fill available space: `scaleFactor = √(containerArea / referenceArea)`

### Interactions

- **Hover a node** → highlights its connections, dims unrelated nodes, shows tooltip with score/weight/description
- **Drag a node** → repositions it (simulation adjusts other nodes)
- **Click a pick card** → swaps the graph to show that pick's factor breakdown
- **Adjust weight sliders** → recalculates confidence in real-time, updates node sizes and edge weights

### Edge Connections

Each factor connects to the probability node (weight = slider weight). Structural edges mirror the model's actual dataflow: form/minutes/volatility feed the Projection node, Projection and the empirical hit rate feed Calibration, and Calibration feeds the final probability.

---

## The Bet Slip & Parlay Builder

### How It Works

1. Click "+" on any pick card to add it to the bet slip
2. The slip shows each leg with player, prop, and individual confidence
3. **Parlay math** (client-side):
   - Combined confidence = product of individual confidences (e.g., 80% × 79% × 76% = 48.0%)
   - Implied American odds from probability: `prob > 0.5 ? -(prob/(1-prob)×100) : +((1-prob)/prob×100)`
4. **Bet size calculator**:
   - Enter your bankroll
   - Suggested wager = confidence-weighted % of bankroll (1% at 50% confidence → 5% at 90%+)
   - Shows "To Win" and "Total Payout" based on implied odds
5. **Correlation warning** — flags when multiple picks are from the same team (correlated outcomes reduce true probability)

---

## Key Technical Highlights for Demo

1. **Backtested, calibrated predictions** — the prop engine's probabilities were validated walk-forward on 530k held-out predictions (Brier 0.153, every calibration bucket within ±0.016); `backtest.py` reproduces it in seconds
2. **Strict no-leakage design** — the backtester computes every input (defense ratings, league averages, splits) only from data available before each game's tip-off
3. **Same code live and in backtest** — the engine is pure computation with no I/O, so the picks API and the backtest exercise the identical code path
4. **Text-to-SQL with error recovery** — if the generated SQL fails, the system asks GPT-4o to fix it based on the error message and retries
5. **Parallel execution everywhere** — `asyncio.gather` runs 6+ database queries simultaneously for betting analysis
6. **RAG with recency boost** — news search uses pgvector cosine similarity but boosts recent articles
7. **No LLM for picks, LLM informed by the model for chat** — the picks engine is deterministic math; the chat pipeline injects the engine's calibrated probability so GPT-4o's betting answers quote a validated model instead of eyeballing hit rates
8. **Safety** — SQL queries run in read-only transactions with 15s timeouts; INSERT/UPDATE/DELETE/DROP are blocked at the application level
9. **Auto-refresh** — news syncs every 15 minutes, scores refresh every 60 seconds

See `docs/INTERVIEW_GUIDE.md` for full interview talking points and `docs/BACKTEST.md` for methodology and results.
