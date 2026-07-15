# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An NBA intelligence assistant: FastAPI + PostgreSQL backend over ~1.45M rows of box scores (2013→present), with an LLM router (OpenAI GPT-4o) for natural-language stats/news/betting questions, and a calibrated probabilistic prop prediction engine validated by a walk-forward backtester. Frontend is vanilla JS served as static files. This is a portfolio/interview project — `PROJECT_OVERVIEW.md` and `docs/INTERVIEW_GUIDE.md` explain how it's presented.

## Commands

```bash
uvicorn app.main:app --reload            # run the API + frontend (http://localhost:8000)
python backtest.py --seasons 2023-24,2024-25              # backtest the prediction engine
python backtest.py --seasons 2022-23 --mode market        # book-style 50/50 lines (hardest test)
python backtest.py --seasons 2021-22,2022-23 --fit-calibration  # refit Platt calibration
python refresh_aggregates.py             # create/refresh the 7 materialized views
python sync_games.py && python sync_players.py && python sync_player_stats.py && python sync_news.py  # data ingestion (idempotent, in this order)
```

- Requires `DATABASE_URL` and `OPENAI_API_KEY` in `.env` (see `.env.example`).
- No test suite or linter is configured. Verification is: run the backtest CLI, hit endpoints with curl, and load the frontend pages.
- `psql` is not on PATH on this machine — use Python (`psycopg2`/`asyncpg`) for ad-hoc DB queries.
- Betting page: `http://localhost:8000/static/betting.html`. Picks API: `GET /betting/picks`. Chat: `POST /ask {"question": ...}`.

## Git conventions (user requirement)

Commit messages: brief, all lowercase. Never add Claude as a contributor (no `Co-Authored-By` trailer). Commit incrementally — each working change — and push to `origin main`.

## Architecture

### Request pipeline (chat)

`app/services/router_service.py` orchestrates every `/ask` request: resolve follow-up context from chat history → normalize nicknames/slang → classify into STATS / NEWS / MIXED / BETTING / OFF_TOPIC → dispatch:

- **STATS** → `stats_service.py`: GPT-4o text-to-SQL (schema + rules in `app/prompts/text_to_sql.py`), execute read-only, format. On SQL error, the error is fed back to the LLM for one repair attempt.
- **NEWS** → `news_service.py`: embed question → pgvector similarity with recency boost → summarize with citations.
- **BETTING** → `betting_service.py`: LLM intent parse (PROP_CHECK / FIND_PICKS / PARLAY / GAME_PREVIEW) → parallel queries via `asyncio.gather` → for prop/parlay intents, injects calibrated engine predictions (`_engine_prediction`) → GPT-4o synthesis with `app/prompts/format_betting.py`.

All prompts live in `app/prompts/` and are written in a quantitative-analyst voice (probabilities, break-even thresholds, sample-size regression) — keep that register when editing them.

### Prediction engine + backtest (the load-bearing design)

`app/services/prediction_engine.py` is **pure computation with no I/O** — deliberately, so the live picks API and `backtest.py` exercise the identical code path. Model: EWMA mean projection → minutes-trend multiplier → damped context adjustments (opponent defense, home/away, B2B) → variance model → continuity-corrected normal CDF → shrinkage blend with L20 hit rate (k=70) → Platt calibration. All tunables live in `ENGINE_PARAMS`; `cal_a`/`cal_b` were fit on 2021-22+2022-23 and validated on 2023-24+2024-25 (Brier 0.153, calibration buckets within ±0.016 — see `docs/BACKTEST.md`).

Rules when touching this area:
- Keep the engine I/O-free; callers fetch data and pass plain lists (stat values most-recent-first).
- `backtest.py` must stay leakage-free: every input (defense ratings, league averages, splits) is computed strictly to-date; same-day games fold into rolling state only after the day rolls over.
- If you change engine math, refit calibration on the train seasons and re-validate on held-out seasons before baking in new constants. Parameter sweeps: pass `params=` and preloaded `rows=` to `run_backtest()` (a season loads once, runs in ~3s).

`app/services/betting_picks_service.py` (powers `/betting/picks`) loads the latest season's logs in one query, builds per-player histories and to-date defense factors in Python, and runs the engine per player. Its response shape (factor keys, graph nodes/edges) is mirrored by hardcoded constants in `frontend/betting.html` (`FACTOR_ORDER`, `FACTOR_LABELS`, `userWeights`) — change both together. Every graph node must be a real model quantity; decorative/fake nodes were deliberately removed.

### Data layer

- `player_game_stats` is the core table; `matchup` strings ('LAL @ BOS' / 'LAL vs. BOS') encode home/away and opponent.
- 7 materialized views (defined in `refresh_aggregates.py`) precompute hit rates, splits, defensive ratings for chat queries. The new picks service reads game logs directly instead.
- Season references use `(SELECT MAX(season_id) FROM player_game_stats)` — never hardcode a season in SQL. Exception: `app/prompts/text_to_sql.py` still hardcodes the current season in its examples; update it when a new season's data lands.
- MVs use `CREATE ... IF NOT EXISTS`: editing a view's SQL in `refresh_aggregates.py` does **not** update an existing view — drop it first.
- SQL safety pattern used everywhere: read-only transactions, 15s timeout, regex block on mutating keywords (`_UNSAFE_PATTERN`).

### Startup behavior

`app/main.py` lifespan: creates the asyncpg pool, runs all `migrations/*.sql` on every startup (they must stay idempotent), and schedules `sync_news.py` as a subprocess every 15 minutes. `app/routers/auth.py` and `conversations.py` exist but are **not mounted** in `main.py` (in-progress feature; migration `001_auth_chat.sql` already creates their tables).

### Offseason gotcha

The live scoreboard (`nba_api`) returns no games in the offseason and the scores fetch logs a noisy (handled) error. `/betting/picks` then falls back to showing top candidates league-wide with no opponent/venue context — this is intended demo behavior, not a bug.
