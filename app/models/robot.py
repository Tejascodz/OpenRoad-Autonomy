from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .delivery import utcnow


class RobotRecord(Base):
    """A robot registered in the fleet (its live state is kept in memory by the fleet manager)."""
    __tablename__ = "robots"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)      # e.g. R001
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    home_lat: Mapped[float] = mapped_column(Float, nullable=False)       # base / charging station
    home_lon: Mapped[float] = mapped_column(Float, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
