import logging
import uuid as _uuid

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_session_token,
    get_current_user_optional,
)
from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _oauth_client(redirect_uri: str) -> AsyncOAuth2Client:
    return AsyncOAuth2Client(
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        redirect_uri=redirect_uri,
        scope="openid email profile",
    )


@router.get("/login")
async def login(request: Request):
    redirect_uri = str(request.url_for("auth_callback"))
    client = _oauth_client(redirect_uri)
    uri, _state = client.create_authorization_url(GOOGLE_AUTHORIZE_URL)
    return RedirectResponse(uri)


@router.get("/callback", name="auth_callback")
async def callback(request: Request):
    redirect_uri = str(request.url_for("auth_callback"))
    client = _oauth_client(redirect_uri)

    token = await client.fetch_token(
        GOOGLE_TOKEN_URL,
        authorization_response=str(request.url),
    )

    resp = await client.get(GOOGLE_USERINFO_URL)
    userinfo = resp.json()

    google_id = userinfo["sub"]
    email = userinfo.get("email", "")
    display_name = userinfo.get("name", "")
    avatar_url = userinfo.get("picture", "")

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO users (google_id, email, display_name, avatar_url, last_login_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (google_id) DO UPDATE
            SET email = EXCLUDED.email,
                display_name = EXCLUDED.display_name,
                avatar_url = EXCLUDED.avatar_url,
                last_login_at = NOW()
        RETURNING id
        """,
        google_id,
        email,
        display_name,
        avatar_url,
    )

    user_id = str(row["id"])
    session_token = create_session_token(user_id)

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@router.get("/me")
async def me(request: Request):
    user_id = await get_current_user_optional(request)
    if not user_id:
        return JSONResponse({"user": None})

    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, display_name, avatar_url FROM users WHERE id = $1",
        _uuid.UUID(user_id),
    )
    if not row:
        return JSONResponse({"user": None})

    return JSONResponse(
        {
            "user": {
                "id": str(row["id"]),
                "email": row["email"],
                "display_name": row["display_name"],
                "avatar_url": row["avatar_url"],
            }
        }
    )


@router.post("/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response
