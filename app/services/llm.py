"""OpenAI client wrapper with usage accounting and a hard daily budget.

Every call is metered. When the daily call or token budget is exhausted the
wrapper raises instead of spending more, so an unattended deployment cannot
run up an open-ended bill.
"""

import logging
import time

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


class LLMBudgetExceeded(RuntimeError):
    """Raised when the daily LLM budget is spent."""


# Rolling daily counters, reset at UTC midnight.
_usage: dict = {
    "day_start": 0.0,
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "embeddings": 0,
    "by_model": {},
}


def _roll_day() -> None:
    now = time.time()
    day_start = now - (now % 86400)
    if _usage["day_start"] != day_start:
        _usage.update({
            "day_start": day_start, "calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "embeddings": 0, "by_model": {},
        })


def _tokens_used() -> int:
    return _usage["prompt_tokens"] + _usage["completion_tokens"]


def _check_budget() -> None:
    _roll_day()
    if _usage["calls"] >= settings.DAILY_LLM_CALL_BUDGET:
        raise LLMBudgetExceeded(
            f"Daily model-call budget reached ({settings.DAILY_LLM_CALL_BUDGET} calls)."
        )
    if _tokens_used() >= settings.DAILY_LLM_TOKEN_BUDGET:
        raise LLMBudgetExceeded(
            f"Daily token budget reached ({settings.DAILY_LLM_TOKEN_BUDGET:,} tokens)."
        )


def _record(model: str, prompt: int, completion: int, is_embedding: bool = False) -> None:
    _roll_day()
    _usage["calls"] += 1
    _usage["prompt_tokens"] += prompt
    _usage["completion_tokens"] += completion
    if is_embedding:
        _usage["embeddings"] += 1
    entry = _usage["by_model"].setdefault(model, {"calls": 0, "tokens": 0})
    entry["calls"] += 1
    entry["tokens"] += prompt + completion


def usage_snapshot() -> dict:
    _roll_day()
    return {
        "calls": _usage["calls"],
        "call_budget": settings.DAILY_LLM_CALL_BUDGET,
        "tokens": _tokens_used(),
        "token_budget": settings.DAILY_LLM_TOKEN_BUDGET,
        "prompt_tokens": _usage["prompt_tokens"],
        "completion_tokens": _usage["completion_tokens"],
        "embedding_calls": _usage["embeddings"],
        "by_model": _usage["by_model"],
        "budget_remaining_calls": max(0, settings.DAILY_LLM_CALL_BUDGET - _usage["calls"]),
    }


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


async def chat_completion(
    messages: list[dict],
    model: str = "gpt-4o",
    temperature: float = 0.0,
    max_tokens: int = 2048,
) -> str:
    _check_budget()
    client = _get_client()
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    usage = getattr(resp, "usage", None)
    _record(model,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0)
    return resp.choices[0].message.content or ""


async def embed_text(text: str, model: str = "text-embedding-3-small") -> list[float]:
    _check_budget()
    client = _get_client()
    resp = await client.embeddings.create(model=model, input=text)
    usage = getattr(resp, "usage", None)
    _record(model, getattr(usage, "prompt_tokens", 0) or 0, 0, is_embedding=True)
    return resp.data[0].embedding
