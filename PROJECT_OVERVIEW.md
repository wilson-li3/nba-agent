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
    ├─ BETTING → parse intent → 6+ parallel DB queries → weighted factor scoring
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
| `app/services/betting_picks_service.py` | **Structured picks engine (no LLM).** Pure computation — scores every player prop using 8 weighted factors, returns structured JSON with graph data for D3 visualization. |

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

## The Betting Engine — How It Works

### Overview

The betting picks page (`/betting/picks`) uses a **pure computation scoring engine** — no LLM calls. It queries the materialized views, scores every candidate prop across 8 factors, and returns structured JSON that the frontend visualizes.

### The 8 Factors

Each player prop is scored 0–1 on these factors:

| Factor | Weight | What It Measures | How It's Scored |
|--------|--------|-----------------|-----------------|
| **Hit Rate L10** | 30% | How often the player hit this line in last 10 games | `games_hit / 10` — e.g., 8/10 = 0.80 |
| **Hit Rate L20** | 15% | Same but last 20 games (more stable sample) | `games_hit / 20` |
| **Trend** | 15% | Is the player trending up or down? | Compares last 5 game avg vs season avg. If L5 > season, score > 0.5 |
| **Consistency** | 10% | How reliable is this player? | Inverse of coefficient of variation: `1 - (stddev / avg)`. Low variance = high score |
| **Matchup** | 10% | How does this player do vs this specific opponent? | Historical avg vs this opponent, compared to their overall avg |
| **Home/Away** | 8% | Home court advantage | Compares home PPG vs away PPG for the game's location |
| **Opp Defense** | 7% | How bad is the opponent's defense? | Percentile rank of opponent's points allowed. Worst defense = 1.0 |
| **Rest (B2B)** | 5% | Is the player on a back-to-back? | 0.35 if B2B (fatigue penalty), 0.65 if rested |

### Confidence Score

```
confidence = Σ (factor_score × factor_weight)
```

All 8 weights sum to 1.0. The frontend lets users adjust these weights with sliders and see confidence recalculate in real-time.

### Filtering

Only shows picks where:
- The player's team plays today
- Hit rate (L10) ≥ 70%
- Confidence ≥ 65%

---

## The D3 Force Graph — How It Works

### What You're Looking At

The graph is an **interactive force-directed network** that visualizes how each factor contributes to a pick's confidence score. Each node is a factor, and the central node is the final confidence.

### Node Types

**9 Real Nodes** (from actual data):
- 1 **Confidence node** (center, large, orange) — the final weighted score
- 8 **Factor nodes** — one per scoring factor (Hit Rate L10, Trend, Matchup, etc.)

**10 Decorative Nodes** (for visual richness):
- Usage Rate, Minutes, Floor/Ceiling, Pace Factor, Injury Signal, Line Movement, Correlation, Recency Bias, Clutch Factor, Venue Effect
- These have pseudo-random values seeded by the pick's data — they make the graph look more like a real analytics network

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

Real edges connect each factor → confidence node (weight = slider weight). Extra edges connect decorative nodes to real factors and each other, creating a dense network appearance. Example: "Pace Factor" connects to "Opp Defense" and "Usage Rate" because pace affects both.

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

1. **Text-to-SQL with error recovery** — if the generated SQL fails, the system asks GPT-4o to fix it based on the error message and retries
2. **Parallel execution everywhere** — `asyncio.gather` runs 6+ database queries simultaneously for betting analysis
3. **RAG with recency boost** — news search uses pgvector cosine similarity but boosts recent articles
4. **No LLM for picks** — the structured picks engine is pure math, making it fast and deterministic
5. **Interactive weight adjustment** — users can tune the 8-factor weights and see confidence recalculate instantly
6. **Safety** — SQL queries run in read-only transactions with 15s timeouts; INSERT/UPDATE/DELETE/DROP are blocked at the application level
7. **Auto-refresh** — news syncs every 15 minutes, scores refresh every 60 seconds
