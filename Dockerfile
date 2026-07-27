# syntax=docker/dockerfile:1
FROM python:3.13-slim

# psycopg2/lxml wheels cover the common cases; curl is here for HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits don't invalidate the install layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code, the static frontend, migrations, and the sync scripts
# (the scheduler shells out to sync_news.py, so it must be in the image).
COPY app/ ./app/
COPY frontend/ ./frontend/
COPY migrations/ ./migrations/
COPY sync_*.py refresh_aggregates.py backtest.py ./

# Never run as root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# sh -c so the platform's injected $PORT is expanded at runtime.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
