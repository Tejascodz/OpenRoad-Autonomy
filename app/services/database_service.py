"""Database access. All queries go through the SQLAlchemy ORM (parameterised - no SQL injection)."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from datetime import timedelta
from functools import lru_cache
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..config import logger, settings
from ..models.base import Base
from ..models.delivery import Delivery, DeliveryStatus, as_utc, utcnow
from ..models.robot import RobotRecord
from ..models.user import AuditLog, Role, User

MAX_PATH_POINTS = 20_000


class DatabaseService:
    def __init__(self, url: str | None = None):
        url = url or settings.DATABASE_URL
        is_sqlite = url.startswith("sqlite")
        kwargs: Dict[str, Any] = {"pool_pre_ping": True}
        if is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
            db_path = url.split("sqlite:///", 1)[-1]
            if db_path and db_path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(db_path)), mode=0o700, exist_ok=True)
        else:
            kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)
        self.engine: Engine = create_engine(url, **kwargs)
        if is_sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        if is_sqlite:
            _restrict_sqlite_file(url)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        # Never log credentials embedded in the URL.
        logger.info("Database ready: %s", self.engine.url.render_as_string(hide_password=True))

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except SQLAlchemyError:
            s.rollback()
            logger.exception("Database error")
            raise
        finally:
            s.close()

    def _add_missing_columns(self) -> None:
        """Tiny additive migration so older databases keep working."""
        insp = inspect(self.engine)
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing or not col.nullable:
                    continue
                ddl_type = col.type.compile(dialect=self.engine.dialect)
                with self.engine.begin() as conn:
                    # identifiers come from our own model metadata, never from user input
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))
                logger.info("Migrated: added column %s.%s", table.name, col.name)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    # ------------------------------------------------------------ deliveries
    def create_delivery(self, pickup_lat: float, pickup_lon: float, delivery_lat: float, delivery_lon: float,
                        path: List, distance_m: float, duration_min: float, *, algorithm: str | None = None,
                        profile: str | None = None, energy_kwh: float | None = None,
                        created_by: int | None = None, robot_id: str | None = None) -> Dict:
        pts = [[round(float(a), 6), round(float(b), 6)] for a, b in path[:MAX_PATH_POINTS]]
        with self.session() as s:
            d = Delivery(pickup_lat=pickup_lat, pickup_lon=pickup_lon, delivery_lat=delivery_lat,
                         delivery_lon=delivery_lon, path_planned=pts, total_distance_m=distance_m,
                         estimated_duration_min=duration_min, status=DeliveryStatus.PENDING,
                         algorithm=algorithm, profile=profile, energy_kwh=energy_kwh, created_by=created_by,
                         robot_id=robot_id)
            s.add(d)
            s.flush()
            return _delivery_dict(d, with_path=False)

    def update_delivery_status(self, delivery_id: int, status: DeliveryStatus) -> None:
        with self.session() as s:
            d = s.get(Delivery, delivery_id)
            if not d:
                return
            d.status = status
            now = utcnow()
            if status == DeliveryStatus.IN_TRANSIT and d.started_at is None:
                d.started_at = now
            elif status in (DeliveryStatus.DELIVERED, DeliveryStatus.COMPLETED, DeliveryStatus.FAILED,
                            DeliveryStatus.CANCELLED):
                if d.completed_at is None:
                    d.completed_at = now
                    if d.started_at:
                        d.actual_duration_min = (now - as_utc(d.started_at)).total_seconds() / 60

    def get_delivery(self, delivery_id: int) -> Optional[Dict]:
        with self.session() as s:
            d = s.get(Delivery, delivery_id)
            return _delivery_dict(d, with_path=True) if d else None

    def get_active_deliveries(self) -> List[Dict]:
        active = [DeliveryStatus.PENDING, DeliveryStatus.PICKUP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.RETURNING]
        with self.session() as s:
            rows = s.scalars(select(Delivery).where(Delivery.status.in_(active)).order_by(Delivery.id.desc()).limit(100))
            return [_delivery_dict(d) for d in rows]

    def get_delivery_history(self, limit: int = 100) -> List[Dict]:
        limit = max(1, min(int(limit), 100))
        with self.session() as s:
            rows = s.scalars(select(Delivery).order_by(Delivery.created_at.desc(), Delivery.id.desc()).limit(limit))
            return [_delivery_dict(d) for d in rows]

    def fail_stale_deliveries(self) -> int:
        """On startup no mission is running, so anything 'active' is stale."""
        with self.session() as s:
            rows = s.scalars(select(Delivery).where(Delivery.status.in_(
                [DeliveryStatus.PENDING, DeliveryStatus.PICKUP, DeliveryStatus.IN_TRANSIT]))).all()
            for d in rows:
                d.status = DeliveryStatus.FAILED
            return len(rows)

    # ------------------------------------------------------------ robots
    def list_robots(self) -> List[Dict]:
        with self.session() as s:
            return [{"id": r.id, "name": r.name, "home_lat": r.home_lat, "home_lon": r.home_lon}
                    for r in s.scalars(select(RobotRecord).where(RobotRecord.active.is_(True)).order_by(RobotRecord.id))]

    def next_robot_id(self) -> str:
        with self.session() as s:
            ids = [r for r in s.scalars(select(RobotRecord.id))]
        n = max([int(i[1:]) for i in ids if i[1:].isdigit()] or [0]) + 1
        return f"R{n:03d}"

    def add_robot(self, rid: str, name: str, home_lat: float, home_lon: float) -> Dict:
        with self.session() as s:
            s.add(RobotRecord(id=rid, name=name, home_lat=home_lat, home_lon=home_lon))
        return {"id": rid, "name": name, "home_lat": home_lat, "home_lon": home_lon}

    def delete_robot(self, rid: str) -> bool:
        with self.session() as s:
            r = s.get(RobotRecord, rid)
            if not r or not r.active:
                return False
            r.active = False  # keep the row so delivery history still resolves the id
            return True

    # ------------------------------------------------------------ users
    def count_users(self, role: Role | None = None, active_only: bool = False) -> int:
        with self.session() as s:
            q = select(func.count(User.id))
            if role:
                q = q.where(User.role == role)
            if active_only:
                q = q.where(User.is_active.is_(True))
            return int(s.scalar(q) or 0)

    def get_user(self, user_id: int) -> Optional[User]:
        with self.session() as s:
            return s.get(User, user_id)

    def get_user_by_name(self, username: str) -> Optional[User]:
        with self.session() as s:
            return s.scalar(select(User).where(func.lower(User.username) == username.lower()))

    def list_users(self) -> List[Dict]:
        with self.session() as s:
            return [u.public() for u in s.scalars(select(User).order_by(User.id))]

    def create_user(self, username: str, password_hash: str, role: Role) -> User:
        with self.session() as s:
            u = User(username=username, password_hash=password_hash, role=role)
            s.add(u)
            s.flush()
            return u

    def update_user(self, user_id: int, **fields: Any) -> Optional[User]:
        """Update fields; any security-relevant change bumps token_version (revokes sessions)."""
        with self.session() as s:
            u = s.get(User, user_id)
            if not u:
                return None
            revoke = False
            for k, v in fields.items():
                if k not in {"role", "is_active", "password_hash", "failed_logins", "locked_until",
                             "last_login_at", "password_changed_at"}:
                    raise ValueError(f"field {k} not updatable")
                if k in {"role", "is_active", "password_hash"} and getattr(u, k) != v:
                    revoke = True
                setattr(u, k, v)
            if revoke:
                u.token_version += 1
            s.flush()
            return u

    def revoke_sessions(self, user_id: int) -> None:
        with self.session() as s:
            u = s.get(User, user_id)
            if u:
                u.token_version += 1

    def delete_user(self, user_id: int) -> bool:
        with self.session() as s:
            u = s.get(User, user_id)
            if not u:
                return False
            s.delete(u)
            return True

    def register_failed_login(self, user_id: int) -> User:
        with self.session() as s:
            u = s.get(User, user_id)
            u.failed_logins += 1
            if u.failed_logins >= settings.LOGIN_MAX_FAILURES:
                u.locked_until = utcnow() + timedelta(minutes=settings.LOGIN_LOCKOUT_MIN)
                u.failed_logins = 0
            s.flush()
            return u

    def register_login(self, user_id: int, new_hash: str | None = None) -> None:
        with self.session() as s:
            u = s.get(User, user_id)
            u.failed_logins = 0
            u.locked_until = None
            u.last_login_at = utcnow()
            if new_hash:
                u.password_hash = new_hash

    # ------------------------------------------------------------ audit
    def audit(self, action: str, *, actor: str | None = None, target: str | None = None,
              ip: str | None = None, success: bool = True, detail: Dict | None = None) -> None:
        try:
            with self.session() as s:
                s.add(AuditLog(actor=actor, action=action[:64], target=(target or "")[:128] or None,
                               ip=(ip or "")[:64] or None, success=success, detail=detail))
        except SQLAlchemyError:
            logger.error("Failed to write audit record for %s", action)

    def list_audit(self, limit: int = 200) -> List[Dict]:
        limit = max(1, min(int(limit), 1000))
        with self.session() as s:
            rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
            return [{"id": a.id, "ts": as_utc(a.ts).isoformat(), "actor": a.actor, "action": a.action,
                     "target": a.target, "ip": a.ip, "success": a.success, "detail": a.detail} for a in rows]


def _iso(dt):
    return as_utc(dt).isoformat() if dt else None


def _delivery_dict(d: Delivery, with_path: bool = False) -> Dict:
    out = {
        "id": d.id,
        "pickup": {"lat": d.pickup_lat, "lon": d.pickup_lon},
        "delivery": {"lat": d.delivery_lat, "lon": d.delivery_lon},
        "status": d.status.value,
        "created_at": _iso(d.created_at),
        "started_at": _iso(d.started_at),
        "completed_at": _iso(d.completed_at),
        "total_distance_m": d.total_distance_m,
        "estimated_duration_min": d.estimated_duration_min,
        "actual_duration_min": d.actual_duration_min,
        "algorithm": d.algorithm,
        "profile": d.profile,
        "energy_kwh": d.energy_kwh,
        "robot_id": d.robot_id,
    }
    if with_path:
        p = d.path_planned
        if isinstance(p, str):  # older rows stored a JSON string inside the JSON column
            import json
            try:
                p = json.loads(p)
            except ValueError:
                p = []
        out["path_planned"] = p or []
    return out


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA secure_delete=ON")
    cur.close()


def _restrict_sqlite_file(url: str) -> None:
    path = url.split("sqlite:///", 1)[-1]
    for p in (path, path + "-wal", path + "-shm"):
        if p and os.path.exists(p):
            try:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 0600: only the service user can read the DB
            except OSError:
                pass


@lru_cache(maxsize=1)
def get_db() -> DatabaseService:
    return DatabaseService()
