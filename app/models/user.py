from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .delivery import as_utc, utcnow


class Role(str, enum.Enum):
    VIEWER = "viewer"      # read-only dashboard, may press EMERGENCY STOP
    OPERATOR = "operator"  # + plan, start / cancel / resume missions
    ADMIN = "admin"        # + user management, audit log, system info

    @property
    def level(self) -> int:
        return {"viewer": 1, "operator": 2, "admin": 3}[self.value]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.VIEWER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=utcnow)

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role.value, "is_active": self.is_active,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
                "locked": bool(self.locked_until and as_utc(self.locked_until) > utcnow())}


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

