import asyncio
import logging

from app.prompts.classify import CLASSIFY_PROMPT

logger = logging.getLogger(__name__)
from app.prompts.normalize import NORMALIZE_PROMPT
from app.prompts.resolve_context import RESOLVE_CONTEXT_PROMPT
from app.services.betting_service import answer_betting_question
from app.services.llm import chat_completion
from app.services.news_service import answer_news_question
from app.services.stats_service import answer_stats_question


async def resolve_context(question: str, message_history: list[dict]) -> str:
    """Rewrite a follow-up question into a standalone question using conversation history."""
    history_lines = []
    for msg in message_history[-10:]:
        role = msg.get("role", "user").capitalize()
        history_lines.append(f"{role}: {msg.get('content', '')}")
    history_text = "\n".join(history_lines)

    prompt = RESOLVE_CONTEXT_PROMPT.format(history=history_text, question=question)
    result = await chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o-mini",
        temperature=0.0,
        max_tokens=200,
    )
    return result.strip()


async def normalize_question(question: str) -> str:
    """Rewrite vague/casual input into a clear, specific question."""
    prompt = NORMALIZE_PROMPT.format(question=question)
    result = await chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o-mini",
        temperature=0.0,
        max_tokens=200,
    )
    return result.strip()


async def classify_question(question: str) -> str:
    """Classify a question as STATS, NEWS, MIXED, BETTING, or OFF_TOPIC."""
    prompt = CLASSIFY_PROMPT.format(question=question)
    result = await chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o-mini",
        temperature=0.0,
        max_tokens=10,
    )
    category = result.strip().upper()
    if category not in ("STATS", "NEWS", "MIXED", "BETTING", "OFF_TOPIC"):
        category = "STATS"
    return category


async def route_question(question: str, message_history: list[dict] | None = None) -> dict:
    """Resolve context if needed, normalize the question, classify it, and dispatch."""
    if message_history:
        question = await resolve_context(question, message_history)
    normalized = await normalize_question(question)
    category = await classify_question(normalized)

    if category == "OFF_TOPIC":
        return {
            "category": "off_topic",
            "original_question": question,
            "answer": "I'm an NBA assistant — my job is for NBA purposes only! Ask me about player stats, game scores, standings, trades, or league news.",
        }

    if category == "STATS":
        result = await answer_stats_question(normalized)
        result["category"] = "stats"
        result["original_question"] = question
        return result

    if category == "NEWS":
        result = await answer_news_question(normalized)
        result["category"] = "news"
        result["original_question"] = question
        return result

    if category == "BETTING":
        # Run betting analysis and news lookup in parallel
        betting_coro = answer_betting_question(normalized)
        news_coro = answer_news_question(normalized)
        betting_result, news_result = await asyncio.gather(betting_coro, news_coro)

        answer = betting_result["answer"]
        sources = news_result.get("sources", [])

        # Append injury/news context if relevant sources were found
        if sources and news_result.get("answer"):
            answer += f"\n\n---\n**Injury/News Watch:**\n{news_result['answer']}"

        return {
            "category": "betting",
            "original_question": question,
            "answer": answer,
            "sources": sources,
        }

    # MIXED — run news first, then pass context to stats
    news_result = await answer_news_question(normalized)
    stats_result = await answer_stats_question(
        normalized, news_context=news_result["answer"]
    )

    combined_answer = (
        f"**Stats perspective:**\n{stats_result['answer']}\n\n"
        f"**News perspective:**\n{news_result['answer']}"
    )
    return {
        "category": "mixed",
        "original_question": question,
        "answer": combined_answer,
        "sql": stats_result.get("sql"),
        "sources": news_result.get("sources", []),
    }
