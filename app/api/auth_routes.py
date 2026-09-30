"""Login / logout / session endpoints."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..models.delivery import as_utc, utcnow
from ..security.deps import Principal, current_principal
from ..security.passwords import hash_password, needs_rehash, password_problems, verify_password
from ..security.rate_limit import RateLimit, client_ip, limiter
from ..security.tokens import ACCESS_COOKIE, CSRF_COOKIE, create_access_token, csrf_for
from ..services.database_service import get_db

router = APIRouter()
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
_BAD_LOGIN = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=1, max_length=128)


def _authenticate(username: str, password: str, ip: str):
    """Blocking: verifies credentials with lockout. Returns User or raises HTTPException."""
    db = get_db()
    if not USERNAME_RE.match(username):
        verify_password(None, password)  # constant-ish time
        raise _BAD_LOGIN
    # per-account throttle in addition to the per-IP one (slows distributed guessing)
    if limiter.hit(f"login-user:{username.lower()}", 10, 300) is not None:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts, try later",
                            headers={"Retry-After": "300"})
    user = db.get_user_by_name(username)
    if user and user.locked_until and as_utc(user.locked_until) > utcnow():
        verify_password(None, password)
        db.audit("auth.login", actor=username, ip=ip, success=False, detail={"reason": "locked"})
        raise HTTPException(status.HTTP_423_LOCKED, "Account temporarily locked. Try again later.")
    ok = verify_password(user.password_hash if user else None, password)
    if not user or not ok or not user.is_active:
        if user and not ok:
            db.register_failed_login(user.id)
        db.audit("auth.login", actor=username, ip=ip, success=False)
        raise _BAD_LOGIN
    db.register_login(user.id, hash_password(password) if needs_rehash(user.password_hash) else None)
    db.audit("auth.login", actor=user.username, ip=ip)
    return user


def _set_session(response: Response, user) -> dict:
    token, claims = create_access_token(user.id, user.role.value, user.token_version)
    csrf = csrf_for(claims["jti"])
    max_age = settings.ACCESS_TOKEN_TTL_MIN * 60
    response.set_cookie(ACCESS_COOKIE, token, max_age=max_age, httponly=True, secure=settings.COOKIE_SECURE,
                        samesite="strict", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=settings.COOKIE_SECURE,
                        samesite="strict", path="/")
    return {"user": {"id": user.id, "username": user.username, "role": user.role.value},
            "csrf_token": csrf, "expires_in": max_age}


@router.post("/login", dependencies=[Depends(RateLimit("login", 5, 60))])
async def login(body: LoginRequest, request: Request, response: Response):
    user = await run_in_threadpool(_authenticate, body.username, body.password, client_ip(request))
    return _set_session(response, user)


@router.post("/token", dependencies=[Depends(RateLimit("login", 5, 60))])
async def token(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    """OAuth2 password flow for scripts/CLI clients: returns a bearer token (no cookies)."""
    if len(form.username) > 64 or len(form.password) > 128:
        raise _BAD_LOGIN
    user = await run_in_threadpool(_authenticate, form.username, form.password, client_ip(request))
    tok, _ = create_access_token(user.id, user.role.value, user.token_version)
    return {"access_token": tok, "token_type": "bearer",  # nosec B105 - OAuth2 token type, not a secret
            "expires_in": settings.ACCESS_TOKEN_TTL_MIN * 60}


@router.post("/logout")
async def logout(request: Request, response: Response, p: Principal = Depends(current_principal)):
    response.delete_cookie(ACCESS_COOKIE, path="/", secure=settings.COOKIE_SECURE, httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.COOKIE_SECURE, samesite="strict")
    await run_in_threadpool(get_db().audit, "auth.logout", actor=p.username, ip=client_ip(request))
    return {"success": True}


@router.post("/logout-all")
async def logout_all(request: Request, response: Response, p: Principal = Depends(current_principal)):
    """Revoke every session of the current user (all devices)."""
    await run_in_threadpool(get_db().revoke_sessions, p.user.id)
    response.delete_cookie(ACCESS_COOKIE, path="/", secure=settings.COOKIE_SECURE, httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.COOKIE_SECURE, samesite="strict")
    await run_in_threadpool(get_db().audit, "auth.logout_all", actor=p.username, ip=client_ip(request))
    return {"success": True}


@router.get("/me")
async def me(p: Principal = Depends(current_principal)):
    return {"id": p.user.id, "username": p.username, "role": p.role.value,
            "csrf_token": csrf_for(p.claims["jti"]) if p.via_cookie else None,
            "expires_at": p.claims["exp"]}


@router.post("/change-password", dependencies=[Depends(RateLimit("change_password", 5, 300))])
async def change_password(body: ChangePasswordRequest, request: Request, response: Response,
                          p: Principal = Depends(current_principal)):
    db = get_db()
    if not await run_in_threadpool(verify_password, p.user.password_hash, body.current_password):
        await run_in_threadpool(db.audit, "auth.change_password", actor=p.username, ip=client_ip(request),
                                success=False)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    problems = password_problems(body.new_password, p.username)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Password " + "; ".join(problems))
    new_hash = await run_in_threadpool(hash_password, body.new_password)
    user = await run_in_threadpool(db.update_user, p.user.id, password_hash=new_hash, password_changed_at=utcnow())
    await run_in_threadpool(db.audit, "auth.change_password", actor=p.username, ip=client_ip(request))
    return _set_session(response, user)  # old sessions are revoked, issue a fresh one
