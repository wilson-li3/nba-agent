import json
import logging
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request

from app.auth import get_current_user_optional
from app.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _require_auth(user_id: str | None) -> _uuid.UUID:
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return _uuid.UUID(user_id)


@router.get("")
async def list_conversations(request: Request):
    user_id = await get_current_user_optional(request)
    uid = _require_auth(user_id)

    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, title, created_at, updated_at
        FROM conversations
        WHERE user_id = $1
        ORDER BY updated_at DESC
        LIMIT 50
        """,
        uid,
    )
    return [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


@router.post("")
async def create_conversation(request: Request):
    user_id = await get_current_user_optional(request)
    uid = _require_auth(user_id)

    body = await request.json()
    title = body.get("title", "New Chat")

    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO conversations (user_id, title) VALUES ($1, $2) RETURNING id, title, created_at, updated_at",
        uid,
        title,
    )
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    user_id = await get_current_user_optional(request)
    uid = _require_auth(user_id)
    conv_id = _uuid.UUID(conversation_id)

    pool = await get_pool()
    conv = await pool.fetchrow(
        "SELECT id, title, user_id, created_at, updated_at FROM conversations WHERE id = $1",
        conv_id,
    )
    if not conv or conv["user_id"] != uid:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = await pool.fetch(
        "SELECT id, role, content, category, sql_query, sources, created_at FROM messages WHERE conversation_id = $1 ORDER BY created_at",
        conv_id,
    )
    return {
        "id": str(conv["id"]),
        "title": conv["title"],
        "created_at": conv["created_at"].isoformat(),
        "updated_at": conv["updated_at"].isoformat(),
        "messages": [
            {
                "id": str(m["id"]),
                "role": m["role"],
                "content": m["content"],
                "category": m["category"],
                "sql_query": m["sql_query"],
                "sources": json.loads(m["sources"]) if m["sources"] else None,
                "created_at": m["created_at"].isoformat(),
            }
            for m in messages
        ],
    }


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request):
    user_id = await get_current_user_optional(request)
    uid = _require_auth(user_id)
    conv_id = _uuid.UUID(conversation_id)

    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM conversations WHERE id = $1 AND user_id = $2",
        conv_id,
        uid,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True}
