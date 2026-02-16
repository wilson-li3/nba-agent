# NBA Intelligence Assistant

An AI-powered NBA assistant that answers questions using a structured stats database and a RAG-based news pipeline, served via a FastAPI API.

## Features

- **Stats queries**: Ask natural language questions about NBA statistics — the system generates SQL, executes it, and returns a formatted answer.
- **News RAG**: Ingests NBA news from RSS feeds, chunks and embeds articles with pgvector, and retrieves relevant context to answer news questions.
- **Intelligent router**: Automatically classifies questions as stats, news, or mixed and dispatches to the appropriate pipeline.

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

# 2. Load teams and players
python sync_players.py

# 3. Load player box scores (bulk, ~160 API calls)
python sync_player_stats.py

# 4. Create/refresh materialized views
python refresh_aggregates.py

# 5. Ingest news articles (RSS → extract → chunk → embed)
python sync_news.py
```

Each script is idempotent (safe to re-run). The news sync also runs automatically every 15 minutes when the API server is running.

## Run the API

```bash
uvicorn app.main:app --reload
```

### Endpoints

- `GET /health` — health check
- `POST /ask` — ask a question

### Example

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Who is the all-time leading scorer?"}'
```

Response:

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
|---|---|
| `games` | One row per NBA game (home/away teams, scores) |
| `teams` | NBA teams reference (id, abbreviation, full name) |
| `players` | All NBA players (historical + active) |
| `player_game_stats` | Individual box scores per player per game |
| `news_articles` | Ingested news articles |
| `news_chunks` | Chunked article text with pgvector embeddings |

### Materialized Views

| View | Description |
|---|---|
| `mv_player_career_totals` | Career sums/averages per player |
| `mv_player_season_averages` | Per-player per-season averages |
| `mv_player_milestone_games` | Milestone counts (40+ pt games, triple-doubles, etc.) |

## Architecture

```
User question
     │
     ▼
 Router (classify: STATS / NEWS / MIXED)
     │
     ├── STATS → text-to-SQL → execute → format answer
     ├── NEWS  → embed query → pgvector search → LLM summarize
     └── MIXED → both in parallel → combined answer
```

## Extending

- Update `CURRENT_SEASON` in `sync_games.py` and `sync_player_stats.py` when a new season starts.
- Add RSS feeds to `RSS_FEEDS` in `sync_news.py`.
