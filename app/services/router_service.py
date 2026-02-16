import asyncio

from app.prompts.classify import CLASSIFY_PROMPT
from app.services.llm import chat_completion
from app.services.news_service import answer_news_question
from app.services.stats_service import answer_stats_question


async def classify_question(question: str) -> str:
    """Classify a question as STATS, NEWS, or MIXED."""
    prompt = CLASSIFY_PROMPT.format(question=question)
    result = await chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o-mini",
        temperature=0.0,
        max_tokens=10,
    )
    category = result.strip().upper()
    if category not in ("STATS", "NEWS", "MIXED", "OFF_TOPIC"):
        category = "STATS"
    return category


async def route_question(question: str) -> dict:
    """Classify the question and dispatch to the appropriate service(s)."""
    category = await classify_question(question)

    if category == "OFF_TOPIC":
        return {
            "category": "off_topic",
            "answer": "I'm an NBA assistant — my job is for NBA purposes only! Ask me about player stats, game scores, standings, trades, or league news.",
        }

    if category == "STATS":
        result = await answer_stats_question(question)
        result["category"] = "stats"
        return result

    if category == "NEWS":
        result = await answer_news_question(question)
        result["category"] = "news"
        return result

    # MIXED — run both in parallel
    stats_task = asyncio.create_task(answer_stats_question(question))
    news_task = asyncio.create_task(answer_news_question(question))
    stats_result, news_result = await asyncio.gather(stats_task, news_task)

    combined_answer = (
        f"**Stats perspective:**\n{stats_result['answer']}\n\n"
        f"**News perspective:**\n{news_result['answer']}"
    )
    return {
        "category": "mixed",
        "answer": combined_answer,
        "sql": stats_result.get("sql"),
        "sources": news_result.get("sources", []),
    }
