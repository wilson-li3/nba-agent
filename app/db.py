import asyncpg

from app.config import settings

pool: asyncpg.Pool | None = None


def _asyncpg_dsn() -> str:
    """Convert DATABASE_URL to asyncpg-compatible DSN if needed."""
    dsn = settings.DATABASE_URL
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgres://", 1)
    return dsn


async def create_pool() -> asyncpg.Pool:
    global pool
    pool = await asyncpg.create_pool(_asyncpg_dsn(), min_size=2, max_size=10)
    return pool


async def close_pool() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


async def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool not initialized")
    return pool
