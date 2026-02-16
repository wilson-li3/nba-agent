import subprocess
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.db import close_pool, create_pool
from app.routers import ask, health

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
