"""Authentication & authorisation dependencies.

A request is authenticated by either
  * the HttpOnly session cookie (browser dashboard) - unsafe methods then also need
    the `X-CSRF-Token` header (session-bound HMAC, see tokens.csrf_for), or
  * an `Authorization: Bearer <token>` header (scripts / API clients; no CSRF needed
    because browsers never attach it automatically).
Every request re-loads the user, so disabling a user / changing their role or
password revokes existing tokens immediately (token_version check).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from ..models.user import Role, User
from ..services.database_service import get_db
from .tokens import ACCESS_COOKIE, CSRF_HEADER, csrf_valid, decode_access_token

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Principal:
    user: User
    claims: Dict[str, Any]
    via_cookie: bool

    @property
    def role(self) -> Role:
        return self.user.role

    @property
    def username(self) -> str:
        return self.user.username


_UNAUTH = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})


def _extract(request_headers, cookies) -> tuple[Optional[str], bool]:
    auth = request_headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip(), False
    tok = cookies.get(ACCESS_COOKIE)
    return (tok, True) if tok else (None, False)


def load_principal(token: str, via_cookie: bool) -> Optional[Principal]:
    """Validate token + user state. Returns None if invalid (blocking: call in a thread)."""
    try:
        claims = decode_access_token(token)
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
    user = get_db().get_user(user_id)
    if user is None or not user.is_active or user.token_version != claims.get("tv"):
        return None
    return Principal(user, claims, via_cookie)


async def current_principal(request: Request) -> Principal:
    token, via_cookie = _extract(request.headers, request.cookies)
    if not token or len(token) > 4096:
        raise _UNAUTH
    p = await run_in_threadpool(load_principal, token, via_cookie)
    if p is None:
        raise _UNAUTH
    if via_cookie and request.method not in SAFE_METHODS:
        if not csrf_valid(p.claims["jti"], request.headers.get(CSRF_HEADER)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
    request.state.principal = p
    return p


def require_role(minimum: Role):
    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role.level < minimum.level:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return p

    return dep


require_viewer = require_role(Role.VIEWER)
require_operator = require_role(Role.OPERATOR)
require_admin = require_role(Role.ADMIN)
