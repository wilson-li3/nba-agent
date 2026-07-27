# Ball Don't Lie — NBA Intelligence Assistant

An AI-powered NBA assistant that answers stats questions via text-to-SQL, retrieves news through a RAG pipeline, analyzes betting props, generates game previews, and streams live scores — all through a conversational chat UI.

## Features

- **Stats Q&A** — Ask natural language questions about NBA statistics. The system generates SQL, executes it, and returns a formatted answer.
- **News RAG** — Ingests NBA news from RSS feeds, chunks and embeds articles with pgvector, and retrieves relevant context to answer news questions.
- **One-screen workspace** — the betting board and the assistant sit side by side: picks and factor analysis on the left, chat docked on the right.
- **Slip simulation** — add picks to the slip and run a 50,000-trial Monte Carlo that prices the parlay with correlation between legs, adjusts for recent news, and reports the break-even odds you need to be profitable.
- **Betting Analysis** — A calibrated probabilistic prediction engine prices player props: recency-weighted mean projection, variance modeling, opponent/venue/rest adjustments, shrinkage, and Platt calibration. Validated with a walk-forward backtest over 880k+ historical predictions (Brier 0.153 on held-out seasons; see `docs/BACKTEST.md`).
- **Backtesting** — `python backtest.py --seasons 2023-24,2024-25` replays history with zero lookahead leakage and reports Brier score, log loss, calibration tables, and simulated ROI.
- **Game Previews** — Click a live-score chip or ask for a matchup to get an AI-generated preview with key stats and storylines.
- **Live Scores** — Displays today's NBA scores (or upcoming games) in a ticker that refreshes every 60 seconds.
- **Headlines Ticker** — Shows the 5 most recent NBA headlines pulled from RSS feeds.
- **Chat UI** — Conversational interface with markdown rendering, category badges, source citations, and conversation history (last 10 messages).
- **Intelligent Router** — Automatically classifies questions as STATS, NEWS, MIXED, BETTING, or OFF_TOPIC and dispatches to the appropriate pipeline.

## Tech Stack

| Layer | Tools |
|-------|-------|
| Backend | Python, FastAPI, asyncpg |
| Database | PostgreSQL, pgvector |
| AI | OpenAI API — GPT-4o, GPT-4o-mini, text-embedding-3-small |
| Frontend | Vanilla JS, marked.js |
| Data sources | nba_api, RSS feeds (ESPN, CBS Sports, Yahoo Sports, Bleacher Report, RealGM, NBA.com) |

## Setup

1. **Create a PostgreSQL database** (with pgvector extension)

   ```bash
   createdb nba
   psql nba -c "CREATE EXTENSION IF NOT EXISTS vector;"
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**

   ```bash
   cp .env.example .env
   # Edit .env: set DATABASE_URL and OPENAI_API_KEY
   ```

## Data Ingestion

Run the sync scripts in order:

```bash
# 1. Load games (all NBA games 1946-present)
python sync_games.py

# 2. Load teams and players (add --bios for active player details)
python sync_players.py

# 3. Load player box scores (bulk, ~160 API calls)
python sync_player_stats.py

# 4. Create/refresh materialized views
python refresh_aggregates.py

# 5. Ingest news articles (RSS -> extract -> chunk -> embed)
python sync_news.py
```

Each script is idempotent (safe to re-run). The news sync also runs automatically every 15 minutes when the API server is running.

## Run the API

```bash
uvicorn app.main:app --reload
```

Then open [http://localhost:8000](http://localhost:8000) to use the chat UI.

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Frontend SPA (chat UI) |
| `/health` | GET | Health check |
| `/ask` | POST | Main Q&A — stats, news, betting, mixed |
| `/scores` | GET | Live / today's NBA scores |
| `/headlines` | GET | Top 5 news headlines |
| `/game-preview` | POST | AI game preview for a matchup |
| `/betting/picks` | GET | Today's calibrated picks with factor attribution |
| `/betting/simulate` | POST | Monte Carlo simulation of a bet slip |
| `/usage` | GET | Today's model spend, token counts, and throttle state |

### Cost controls

The app is unauthenticated, so before exposing it publicly note the two guards
that cap OpenAI spend:

- **Weighted rate limiting** per client IP — routes cost credits proportional to
  how many model calls they trigger (`/betting/simulate` 10, `/ask` 5, `/scores`
  free), enforced over a rolling minute and a day.
- **A daily budget kill switch** — every call's tokens are metered; once
  `DAILY_LLM_CALL_BUDGET` or `DAILY_LLM_TOKEN_BUDGET` is reached the app stops
  calling OpenAI and explains itself instead of failing.

- **A cap on background ingestion** — `sync_news.py` runs on a schedule and
  embeds new articles, which is spend that happens with zero users. It is a
  separate process from the API, so it carries its own daily limit
  (`NEWS_MAX_ARTICLES_PER_DAY`, default 250).

Tune with env vars (`RATE_LIMIT_CREDITS`, `RATE_LIMIT_WINDOW_SECONDS`,
`RATE_LIMIT_DAILY_CREDITS`, `DAILY_LLM_CALL_BUDGET`, `DAILY_LLM_TOKEN_BUDGET`,
`NEWS_MAX_ARTICLES_PER_DAY`) and monitor `GET /usage`.

### Example

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Who is the all-time leading scorer?"}'
```

```json
{
  "question": "Who is the all-time leading scorer?",
  "category": "stats",
  "answer": "LeBron James is the NBA's all-time leading scorer with 40,474 total points...",
  "sql": "SELECT display_name, total_pts FROM mv_player_career_totals ORDER BY total_pts DESC LIMIT 5;"
}
```

## Database Schema

### Tables

| Table | Description |
|-------|-------------|
| `games` | One row per NBA game (home/away teams, scores, 1946-present) |
| `teams` | NBA teams reference (id, abbreviation, full name) |
| `players` | All NBA players — historical + active (with bios for active) |
| `player_game_stats` | Individual box scores per player per game |
| `news_articles` | Ingested news articles |
| `news_chunks` | Chunked article text with pgvector embeddings |

### Materialized Views

| View | Description |
|------|-------------|
| `mv_player_career_totals` | Career sums and averages per player |
| `mv_player_season_averages` | Per-player per-season averages |
| `mv_player_milestone_games` | Milestone counts (40+ pt games, triple-doubles, etc.) |
| `mv_team_back_to_backs` | Back-to-back game detection |
| `mv_player_prop_hit_rates` | Prop threshold hit rates (last 10/20 games) |
| `mv_player_home_away_splits` | Home vs away performance splits |
| `mv_team_defensive_ratings` | Opponent scoring allowed per team |

## Architecture

```
User question
     │
     ▼
 Context Resolution (rewrite follow-ups into standalone questions)
     │
     ▼
 Normalizer (clarify vague input)
     │
     ▼
 Router (classify: STATS / NEWS / MIXED / BETTING / OFF_TOPIC)
     │
     ├── STATS   → text-to-SQL → execute → format answer
     ├── NEWS    → embed query → pgvector search → LLM summarize
     ├── MIXED   → STATS + NEWS in parallel → combined answer
     ├── BETTING → prop hit-rates + splits + defense + news → analysis
     └── OFF_TOPIC → polite rejection

 Game Preview (separate endpoint)
     └── team stats + recent form + head-to-head + news → preview
```

## Extending

- Update `CURRENT_SEASON` in `sync_games.py` and `sync_player_stats.py` when a new season starts.
- Add RSS feeds to `RSS_FEEDS` in `sync_news.py`.

## Roadmap

- **Redis caching** — Cache chat sessions and API responses for faster repeat queries
- **Persistent chat history** — Store conversation history in PostgreSQL
- **Docker** — Dockerfile + docker-compose for local development
- **AWS deployment** — ECS Fargate, RDS, ElastiCache, ECR, GitHub Actions CI/CD
