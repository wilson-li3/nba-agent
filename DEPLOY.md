# Deploying

The app is a stateful service, not a static site: it needs a persistent Python
process, PostgreSQL **with the `vector` and `unaccent` extensions**, and
outbound HTTPS to OpenAI, the NBA API, and RSS feeds. GitHub Pages and other
static hosts cannot run it.

Everything is packaged with Docker, so any container host works and you are not
locked into one platform's config format.

## Run the whole stack locally

```bash
cp .env.example .env          # set OPENAI_API_KEY
docker compose up --build     # API on http://localhost:8099
```

Compose starts `pgvector/pgvector:pg17` alongside the API and creates both
extensions on first boot (`docker/init-db.sql`). The host ports are deliberately
offset — API on **8099**, Postgres on **5433** — so the stack never collides
with a local `uvicorn --port 8000` or a system Postgres on 5432.

## Deploying the image

The container reads `$PORT` at runtime, so it drops into any of these:

| Host | Notes |
|---|---|
| **Render** | Simplest path. Web Service from the Dockerfile + Render Postgres (supports pgvector). Push-to-deploy is the CD half of the pipeline. Free web instances spin down when idle, and a ~50s cold start makes a demo look broken — use a paid instance for anything you're sharing. |
| **Railway** | Usage-based, very little config, Postgres available as a service. |
| **Fly.io** | Cheapest at scale and the most control; needs `fly.toml` and you manage Postgres yourself. |
| **Cloud Run / ECS** | Fine, but pair with a managed Postgres (Neon, Supabase, RDS). Scale-to-zero breaks the in-process scheduler — see below. |

Required environment variables:

```
DATABASE_URL=postgresql://user:pass@host:5432/nba
OPENAI_API_KEY=sk-...
# optional, see README "Cost controls"
RATE_LIMIT_CREDITS, RATE_LIMIT_DAILY_CREDITS,
DAILY_LLM_CALL_BUDGET, DAILY_LLM_TOKEN_BUDGET,
NEWS_MAX_ARTICLES_PER_DAY
```

Point the platform's health check at **`/health`**.

## Seeding the database (the step people forget)

A fresh database is empty, and the app returns an empty board until it has box
scores. **Do not re-run the sync scripts against production** — that is
thousands of NBA API calls and takes hours. Dump and restore instead:

```bash
# from the machine that already has the data
pg_dump --no-owner --no-acl "$LOCAL_DATABASE_URL" | psql "$PROD_DATABASE_URL"

# then rebuild the materialized views on the target
DATABASE_URL="$PROD_DATABASE_URL" python refresh_aggregates.py
```

The dump is roughly **490 MB** (355 MB box scores, 93 MB news embeddings), which
is above the free tier of most managed Postgres providers — budget for a paid
database. To trim it, prune `news_chunks` older than ~30 days; news search
already boosts recency, so old embeddings earn little.

## The scheduler and scale-to-zero

`app/main.py` runs APScheduler in-process and shells out to `sync_news.py` every
15 minutes. That works on an always-on instance. If your host scales to zero,
the schedule silently stops running: remove the job and drive `sync_news.py`
from the platform's own cron instead.

Either way that script has its own daily cap (`NEWS_MAX_ARTICLES_PER_DAY`),
because it runs as a separate process and cannot see the API's in-memory LLM
budget.

## CI

`.github/workflows/ci.yml` runs on every push and PR:

- **Unit tests** over the prediction engine and the copula simulation — both are
  pure computation, so they need no database and no API keys.
- **Import check** across the app and the sync scripts.
- **Frontend JS syntax** via `scripts/check_frontend_js.py`, which extracts the
  inline `<script>` blocks and runs `node --check`. The frontend is one large
  HTML file, so a typo there is invisible to Python tooling and ships a blank
  page.
- **Docker build**, so a broken Dockerfile fails the PR rather than the deploy.

Adding CD is then one step: have your host watch `main` (Render and Railway do
this natively), or add a job that pushes the image to a registry and calls the
host's deploy hook.

## Before going public

- Rate limiting and the daily model budget are described in the README under
  **Cost controls**. Set a hard spend limit in the OpenAI dashboard too, and
  check whether auto-recharge is enabled.
- There is **no authentication** — the auth router exists but is not mounted, so
  every endpoint is public and the throttle is per-IP.
- Rate-limit buckets are per-process. With more than one worker each gets its
  own allowance; move them to Redis or Postgres if you scale out.
