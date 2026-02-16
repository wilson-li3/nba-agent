import asyncio
import re

from app.db import get_pool
from app.prompts.format_stats import FORMAT_STATS_PROMPT
from app.prompts.text_to_sql import SCHEMA_DESCRIPTION, TEXT_TO_SQL_PROMPT
from app.services.llm import chat_completion

# Disallowed SQL patterns (case-insensitive)
_UNSAFE_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|EXECUTE)\b",
    re.IGNORECASE,
)


async def answer_stats_question(question: str, news_context: str | None = None) -> dict:
    """Full text-to-SQL pipeline: generate SQL, execute safely, format response."""
    # Step 1: Generate SQL
    prompt = TEXT_TO_SQL_PROMPT.format(schema=SCHEMA_DESCRIPTION, question=question)
    if news_context:
        prompt += f"\n\nRelevant injury/news context:\n{news_context}"
    sql = await chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o",
        temperature=0.0,
    )
    sql = sql.strip().strip("`").strip()
    if sql.startswith("sql"):
        sql = sql[3:].strip()

    # Step 2: Safety check
    if _UNSAFE_PATTERN.search(sql):
        return {
            "answer": "I can only run read-only queries. Your question would require modifying the database.",
            "sql": sql,
            "results": None,
        }

    # Step 3: Execute with read-only transaction and timeout (retry once on error)
    pool = await get_pool()
    last_error = None
    for attempt in range(2):
        try:
            async with pool.acquire() as conn:
                async with conn.transaction(readonly=True):
                    rows = await asyncio.wait_for(
                        conn.fetch(sql),
                        timeout=15.0,
                    )
                    results = [dict(r) for r in rows]
            last_error = None
            break
        except asyncio.TimeoutError:
            return {
                "answer": "The query took too long to execute. Try a more specific question.",
                "sql": sql,
                "results": None,
            }
        except Exception as e:
            last_error = e
            if attempt == 0:
                # Ask the LLM to fix the SQL based on the error
                retry_prompt = (
                    f"The following SQL query failed with this error:\n\n"
                    f"SQL:\n{sql}\n\n"
                    f"Error:\n{e}\n\n"
                    f"Original question: {question}\n\n"
                    f"{SCHEMA_DESCRIPTION}\n\n"
                    f"Please fix the SQL query. Output ONLY the corrected SQL, no explanation."
                )
                sql = await chat_completion(
                    messages=[{"role": "user", "content": retry_prompt}],
                    model="gpt-4o",
                    temperature=0.0,
                )
                sql = sql.strip().strip("`").strip()
                if sql.startswith("sql"):
                    sql = sql[3:].strip()
                if _UNSAFE_PATTERN.search(sql):
                    return {
                        "answer": "I can only run read-only queries. Your question would require modifying the database.",
                        "sql": sql,
                        "results": None,
                    }

    if last_error is not None:
        return {
            "answer": f"Error executing query: {last_error}",
            "sql": sql,
            "results": None,
        }

    # Step 4: Format results into natural language
    results_str = str(results[:25]) if results else "No results found."
    format_prompt = FORMAT_STATS_PROMPT.format(
        question=question, sql=sql, results=results_str
    )
    answer = await chat_completion(
        messages=[{"role": "user", "content": format_prompt}],
        model="gpt-4o-mini",
        temperature=0.2,
    )

    return {
        "answer": answer,
        "sql": sql,
        "results": results[:25] if results else [],
    }
