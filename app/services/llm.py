from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None


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
    client = _get_client()
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


async def embed_text(text: str, model: str = "text-embedding-3-small") -> list[float]:
    client = _get_client()
    resp = await client.embeddings.create(model=model, input=text)
    return resp.data[0].embedding
