from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://user:password@localhost:5432/nba"
    OPENAI_API_KEY: str = ""
    FRONTEND_URL: str = "http://localhost:8000"

    # ── Spend protection ──────────────────────────────────────────────────
    # Per-client throttle. Costs are credits, not requests: see app/rate_limit.py
    RATE_LIMIT_CREDITS: int = 40           # per window, per client
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    RATE_LIMIT_DAILY_CREDITS: int = 600    # per client, per day

    # Global kill switch across all clients. Whichever trips first wins, and
    # the app degrades to a friendly message instead of billing further.
    DAILY_LLM_CALL_BUDGET: int = 3000
    DAILY_LLM_TOKEN_BUDGET: int = 3_000_000

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
