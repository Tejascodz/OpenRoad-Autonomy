"""Signed session tokens (JWT, HS256) and session-bound CSRF tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import jwt

from ..config import settings

ALGORITHM = "HS256"
ISSUER = "openroad-autonomy"
AUDIENCE = "openroad-api"
ACCESS_COOKIE = "or_session"
CSRF_COOKIE = "or_csrf"
CSRF_HEADER = "x-csrf-token"


def _key() -> str:
    return settings.SECRET_KEY.get_secret_value()


def create_access_token(user_id: int, role: str, token_version: int) -> tuple[str, Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "role": role,
        "tv": token_version,
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "typ": "access",
    }
    return jwt.encode(claims, _key(), algorithm=ALGORITHM), claims


def decode_access_token(token: str) -> Dict[str, Any]:
    """Raises jwt.PyJWTError on any problem (bad signature, expired, wrong aud/iss, alg=none...)."""
    claims = jwt.decode(
        token, _key(), algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER, leeway=10,
        options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
    )
    if claims.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return claims


def csrf_for(jti: str) -> str:
    """CSRF token derived from the session id: cannot be forged or planted by another site."""
    mac = hmac.new(_key().encode(), f"csrf:{jti}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def csrf_valid(jti: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(csrf_for(jti), supplied)
