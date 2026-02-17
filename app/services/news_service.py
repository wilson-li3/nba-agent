from app.db import get_pool
from app.prompts.summarize_news import SUMMARIZE_NEWS_PROMPT
from app.services.llm import chat_completion, embed_text


async def answer_news_question(question: str) -> dict:
    """Embed query, search pgvector for similar chunks, summarize with LLM."""
    # Step 1: Embed the question
    query_embedding = await embed_text(question)
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    # Step 2: Find top 5 most similar chunks
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                nc.content,
                na.title,
                na.source,
                na.url,
                nc.embedding <=> $1::vector AS distance
            FROM news_chunks nc
            JOIN news_articles na ON nc.article_id = na.article_id
            ORDER BY
              (nc.embedding <=> $1::vector) *
              (1.0 + GREATEST(0, EXTRACT(EPOCH FROM (NOW() - COALESCE(na.published_at, na.ingested_at)))) / 86400.0 * 0.02)
            LIMIT 5
        """, embedding_str)

    if not rows:
        return {
            "answer": "I don't have any recent news articles to answer that question. "
                      "Try running sync_news.py first to ingest news.",
            "sources": [],
        }

    # Step 3: Format chunks for the prompt
    chunks_text = ""
    sources = []
    for i, row in enumerate(rows, 1):
        chunks_text += f"\n--- Excerpt {i} (from: {row['title']}, source: {row['source']}) ---\n"
        chunks_text += row["content"] + "\n"
        sources.append({"title": row["title"], "url": row["url"], "source": row["source"]})

    # Step 4: Summarize with LLM
    prompt = SUMMARIZE_NEWS_PROMPT.format(question=question, chunks=chunks_text)
    answer = await chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o-mini",
        temperature=0.2,
    )

    return {
        "answer": answer,
        "sources": sources,
    }
