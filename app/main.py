import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import rate_limit
from app.db import close_pool, create_pool, get_pool
from app.routers import ask, betting, game_preview, health, headlines, scores
from app.services.llm import LLMBudgetExceeded

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

scheduler = AsyncIOScheduler()


def run_sync_news():
    """Run sync_news.py as a subprocess."""
    subprocess.Popen([sys.executable, "sync_news.py"])


async def run_migrations():
    """Run all SQL migration files on startup."""
    pool = await get_pool()
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = sql_file.read_text()
        await pool.execute(sql)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool()
    await run_migrations()
    scheduler.add_job(run_sync_news, "interval", minutes=15, id="sync_news")
    scheduler.start()
    yield
    scheduler.shutdown()
    await close_pool()


app = FastAPI(title="NBA Intelligence Assistant", lifespan=lifespan)

@app.middleware("http")
async def throttle(request: Request, call_next):
    """Weighted per-client rate limit — see app/rate_limit.py for the costs."""
    allowed, message, retry_after = rate_limit.check(request)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"error": message, "answer": message},
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


@app.exception_handler(LLMBudgetExceeded)
async def budget_exceeded(request: Request, exc: LLMBudgetExceeded):
    """Global kill switch tripped: stop spending, explain, stay up."""
    message = (
        "This demo has reached its daily model budget, so live answers are paused "
        "until it resets at midnight UTC. Everything else still works."
    )
    return JSONResponse(status_code=429,
                        content={"error": str(exc), "answer": message},
                        headers={"Retry-After": "3600"})


app.include_router(health.router)
app.include_router(ask.router)
app.include_router(headlines.router)
app.include_router(scores.router)
app.include_router(game_preview.router)
app.include_router(betting.router)


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
