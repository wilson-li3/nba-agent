import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import close_pool, create_pool
from app.routers import ask, health

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

scheduler = AsyncIOScheduler()


def run_sync_news():
    """Run sync_news.py as a subprocess."""
    subprocess.Popen([sys.executable, "sync_news.py"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool()
    scheduler.add_job(run_sync_news, "interval", minutes=15, id="sync_news")
    scheduler.start()
    yield
    scheduler.shutdown()
    await close_pool()


app = FastAPI(title="NBA Intelligence Assistant", lifespan=lifespan)

app.include_router(health.router)
app.include_router(ask.router)


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
