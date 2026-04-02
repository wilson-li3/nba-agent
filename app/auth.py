from fastapi import Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import settings

_serializer = URLSafeTimedSerializer(settings.SESSION_SECRET_KEY)
SESSION_COOKIE = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def create_session_token(user_id: str) -> str:
    return _serializer.dumps(user_id)


def read_session_token(token: str) -> str | None:
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


async def get_current_user_optional(request: Request) -> str | None:
    """Return user UUID string from session cookie, or None for guests."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return read_session_token(token)
