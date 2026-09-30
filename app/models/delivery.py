from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo on round-trip; treat naive timestamps as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DeliveryStatus(enum.Enum):
    PENDING = "pending"
    PICKUP = "pickup"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    RETURNING = "returning"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pickup_lat: Mapped[float] = mapped_column(Float, nullable=False)
    pickup_lon: Mapped[float] = mapped_column(Float, nullable=False)
    delivery_lat: Mapped[float] = mapped_column(Float, nullable=False)
    delivery_lon: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus), default=DeliveryStatus.PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    path_planned: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)  # [[lat, lon], ...]
    total_distance_m: Mapped[Optional[float]] = mapped_column(Float)
    estimated_duration_min: Mapped[Optional[float]] = mapped_column(Float)
    actual_duration_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # v2 columns (added automatically to existing databases by DatabaseService)
    algorithm: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    energy_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    robot_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
