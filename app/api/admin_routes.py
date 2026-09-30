"""Administrator-only endpoints: user management and audit log."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from ..models.delivery import utcnow
from ..models.user import Role
from ..security.deps import Principal, require_admin
from ..security.passwords import hash_password, password_problems
from ..security.rate_limit import RateLimit, client_ip
from ..services.database_service import get_db
from ..services.fleet import MissionError, home_positions
from ..state import fleet

router = APIRouter(dependencies=[Depends(require_admin), Depends(RateLimit("admin", 60, 60))])
UserId = Path(..., ge=1, le=2**31 - 1)


class CreateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., pattern=r"^[A-Za-z0-9_.-]{3,64}$")
    password: str = Field(..., min_length=1, max_length=128)
    role: Role = Role.VIEWER


class UpdateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Optional[Role] = None
    is_active: Optional[bool] = None


class ResetPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(..., min_length=1, max_length=128)


def _check_password(pw: str, username: str) -> None:
    problems = password_problems(pw, username)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Password " + "; ".join(problems))


def _would_remove_last_admin(user_id: int, new_role: Optional[Role], deactivate: bool) -> bool:
    db = get_db()
    u = db.get_user(user_id)
    if not u or u.role != Role.ADMIN or not u.is_active:
        return False
    demoting = new_role is not None and new_role != Role.ADMIN
    return (demoting or deactivate) and db.count_users(Role.ADMIN, active_only=True) <= 1


@router.get("/users")
def list_users():
    return get_db().list_users()


@router.post("/users", status_code=201)
async def create_user(body: CreateUser, request: Request, p: Principal = Depends(require_admin)):
    _check_password(body.password, body.username)
    db = get_db()
    if await run_in_threadpool(db.get_user_by_name, body.username):
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already exists")
    try:
        u = await run_in_threadpool(db.create_user, body.username, await run_in_threadpool(hash_password, body.password),
                                    body.role)
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already exists")
    await run_in_threadpool(db.audit, "admin.user_create", actor=p.username, target=body.username,
                            ip=client_ip(request), detail={"role": body.role.value})
    return u.public()


@router.patch("/users/{user_id}")
async def update_user(body: UpdateUser, request: Request, user_id: int = UserId, p: Principal = Depends(require_admin)):
    db = get_db()
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Nothing to update")
    if user_id == p.user.id and ("role" in fields or fields.get("is_active") is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot change your own role or disable yourself")
    if await run_in_threadpool(_would_remove_last_admin, user_id, fields.get("role"), fields.get("is_active") is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot remove the last active admin")
    u = await run_in_threadpool(lambda: db.update_user(user_id, **fields))
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await run_in_threadpool(db.audit, "admin.user_update", actor=p.username, target=u.username,
                            ip=client_ip(request), detail={k: getattr(v, "value", v) for k, v in fields.items()})
    return u.public()


@router.post("/users/{user_id}/reset-password")
async def reset_password(body: ResetPassword, request: Request, user_id: int = UserId,
                         p: Principal = Depends(require_admin)):
    db = get_db()
    u = await run_in_threadpool(db.get_user, user_id)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    _check_password(body.new_password, u.username)
    h = await run_in_threadpool(hash_password, body.new_password)
    await run_in_threadpool(lambda: db.update_user(user_id, password_hash=h, password_changed_at=utcnow(),
                                                   failed_logins=0, locked_until=None))
    await run_in_threadpool(db.audit, "admin.password_reset", actor=p.username, target=u.username, ip=client_ip(request))
    return {"success": True}


@router.post("/users/{user_id}/unlock")
async def unlock_user(request: Request, user_id: int = UserId, p: Principal = Depends(require_admin)):
    db = get_db()
    u = await run_in_threadpool(lambda: db.update_user(user_id, failed_logins=0, locked_until=None))
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await run_in_threadpool(db.audit, "admin.user_unlock", actor=p.username, target=u.username, ip=client_ip(request))
    return u.public()


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_sessions(request: Request, user_id: int = UserId, p: Principal = Depends(require_admin)):
    db = get_db()
    u = await run_in_threadpool(db.get_user, user_id)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await run_in_threadpool(db.revoke_sessions, user_id)
    await run_in_threadpool(db.audit, "admin.revoke_sessions", actor=p.username, target=u.username, ip=client_ip(request))
    return {"success": True}


@router.delete("/users/{user_id}")
async def delete_user(request: Request, user_id: int = UserId, p: Principal = Depends(require_admin)):
    db = get_db()
    if user_id == p.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete yourself")
    u = await run_in_threadpool(db.get_user, user_id)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if await run_in_threadpool(_would_remove_last_admin, user_id, None, True):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot remove the last active admin")
    await run_in_threadpool(db.delete_user, user_id)
    await run_in_threadpool(db.audit, "admin.user_delete", actor=p.username, target=u.username, ip=client_ip(request))
    return {"success": True}


@router.get("/audit")
def audit_log(limit: int = Query(200, ge=1, le=1000)):
    return get_db().list_audit(limit)


# ---------------------------------------------------------------- robots
class CreateRobot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9 _.-]{1,40}$")


@router.get("/robots")
def list_robots():
    return get_db().list_robots()


@router.post("/robots", status_code=201)
async def add_robot(body: CreateRobot, request: Request, p: Principal = Depends(require_admin)):
    from ..config import settings
    if len(fleet.robots) >= 20:
        raise HTTPException(status.HTTP_409_CONFLICT, "Fleet limit reached (20 robots)")
    db = get_db()
    rid = await run_in_threadpool(db.next_robot_id)
    center = (settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON)
    n = len(fleet.robots)
    home = home_positions(n + 1, center)[n]
    name = body.name or f"Robot {rid[1:].lstrip('0') or '0'}"
    rec = await run_in_threadpool(db.add_robot, rid, name, home[0], home[1])
    fleet.add(rid, name, home)
    fleet.log("info", f"{rid} added to the fleet", rid)
    await run_in_threadpool(db.audit, "admin.robot_add", actor=p.username, target=rid, ip=client_ip(request))
    return rec


@router.delete("/robots/{rid}")
async def remove_robot(request: Request, rid: str = Path(..., pattern=r"^R\d{3}$"),
                       p: Principal = Depends(require_admin)):
    if rid not in fleet.robots:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Robot not found")
    if len(fleet.robots) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The fleet needs at least one robot")
    try:
        fleet.remove(rid)
    except MissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    await run_in_threadpool(get_db().delete_robot, rid)
    fleet.log("warning", f"{rid} removed from the fleet", rid)
    await run_in_threadpool(get_db().audit, "admin.robot_remove", actor=p.username, target=rid, ip=client_ip(request))
    return {"success": True}
