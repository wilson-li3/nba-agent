"""
Fetch NBA news from RSS feeds, extract article text, chunk, embed, and store in pgvector.
Run: python sync_news.py
Requires: DATABASE_URL, OPENAI_API_KEY in environment or .env
"""

import os
import time
from datetime import datetime, timezone

import feedparser
import psycopg2
import tiktoken
import trafilatura
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

RSS_FEEDS = [
    ("ESPN NBA", "https://www.espn.com/espn/rss/nba/news"),
    ("RealGM NBA", "https://basketball.realgm.com/rss/wiretap/0/0.xml"),
    ("CBS Sports NBA", "https://www.cbssports.com/rss/headlines/nba/"),
    ("Yahoo Sports NBA", "https://sports.yahoo.com/nba/rss.xml"),
    ("Bleacher Report", "https://bleacherreport.com/nba.rss"),
    ("NBA.com", "https://www.nba.com/news/rss.xml"),
]

CHUNK_SIZE = 500       # tokens
CHUNK_OVERLAP = 50     # tokens
EMBEDDING_MODEL = "text-embedding-3-small"
REQUEST_DELAY_SEC = 0.3


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news_articles (
                article_id   SERIAL PRIMARY KEY,
                title        TEXT NOT NULL,
                url          TEXT UNIQUE NOT NULL,
                source       TEXT,
                published_at TIMESTAMPTZ,
                ingested_at  TIMESTAMPTZ DEFAULT NOW(),
                full_text    TEXT
            );
        """)
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news_chunks (
                chunk_id    SERIAL PRIMARY KEY,
                article_id  INT NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
                chunk_index INT NOT NULL,
                content     TEXT NOT NULL,
                embedding   vector(1536) NOT NULL
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_embedding
            ON news_chunks USING hnsw (embedding vector_cosine_ops);
        """)
        conn.commit()


def chunk_text(text: str, enc: tiktoken.Encoding) -> list[str]:
    """Split text into chunks of ~CHUNK_SIZE tokens with CHUNK_OVERLAP overlap."""
    tokens = enc.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = start + CHUNK_SIZE
        chunk_tokens = tokens[start:end]
        chunks.append(enc.decode(chunk_tokens))
        start = end - CHUNK_OVERLAP
    return chunks


def get_embedding(client: OpenAI, text: str) -> list[float]:
    time.sleep(REQUEST_DELAY_SEC)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def parse_published(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    openai_key = os.getenv("OPENAI_API_KEY")
    if not database_url:
        raise SystemExit("Set DATABASE_URL in .env")
    if not openai_key:
        raise SystemExit("Set OPENAI_API_KEY in .env")

    conn = psycopg2.connect(database_url)
    ensure_schema(conn)

    client = OpenAI(api_key=openai_key)
    enc = tiktoken.encoding_for_model("gpt-4o")

    total_articles = 0
    total_chunks = 0

    for source_name, feed_url in RSS_FEEDS:
        print(f"Fetching {source_name} ...", flush=True)
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"  Error parsing feed: {e}")
            continue

        for entry in feed.entries:
            url = getattr(entry, "link", None)
            title = getattr(entry, "title", "Untitled")
            if not url:
                continue

            # Check if already ingested
            with conn.cursor() as cur:
                cur.execute("SELECT article_id FROM news_articles WHERE url = %s", (url,))
                if cur.fetchone():
                    continue

            # Extract full article text
            try:
                downloaded = trafilatura.fetch_url(url)
                full_text = trafilatura.extract(downloaded) if downloaded else None
            except Exception:
                full_text = None

            if not full_text or len(full_text.strip()) < 100:
                continue

            published_at = parse_published(entry)

            # Insert article
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO news_articles (title, url, source, published_at, full_text)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING article_id
                """, (title, url, source_name, published_at, full_text))
                article_id = cur.fetchone()[0]
                conn.commit()

            # Chunk and embed
            chunks = chunk_text(full_text, enc)
            for i, chunk_text_str in enumerate(chunks):
                embedding = get_embedding(client, chunk_text_str)
                embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO news_chunks (article_id, chunk_index, content, embedding)
                        VALUES (%s, %s, %s, %s::vector)
                    """, (article_id, i, chunk_text_str, embedding_str))
                conn.commit()
                total_chunks += 1

            total_articles += 1
            print(f"  {title[:60]}... ({len(chunks)} chunks)")

    conn.close()
    print(f"Done. Ingested {total_articles} articles, {total_chunks} chunks.")


if __name__ == "__main__":
    main()
