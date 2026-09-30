"""Application configuration.

All secrets come from environment variables (or a local, git-ignored `.env`).
Nothing secret is ever hard-coded here. `python scripts/setup_env.py` generates a
`.env` with fresh random secrets.
"""
from __future__ import annotations

import logging
import secrets
import sys
from pathlib import Path
from typing import Annotated, List, Literal, Optional

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
_WEAK_SECRETS = {"", "changeme", "change-me", "secret", "your-secret-key", "dev", "test"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ---- Environment ----------------------------------------------------
    ENVIRONMENT: Literal["development", "production", "test"] = "development"
    DEBUG: bool = False  # never enable in production (validated below)

    # ---- Secrets (never logged, never returned by the API) --------------
    SECRET_KEY: SecretStr = SecretStr("")
    BOOTSTRAP_ADMIN_USERNAME: str = "admin"
    BOOTSTRAP_ADMIN_PASSWORD: Optional[SecretStr] = None

    # ---- Auth / sessions ------------------------------------------------
    ACCESS_TOKEN_TTL_MIN: int = Field(30, ge=5, le=24 * 60)
    COOKIE_SECURE: bool = True
    LOGIN_MAX_FAILURES: int = Field(5, ge=3, le=20)
    LOGIN_LOCKOUT_MIN: int = Field(15, ge=1, le=24 * 60)
    PASSWORD_MIN_LENGTH: int = Field(12, ge=10, le=128)

    # ---- HTTP -----------------------------------------------------------
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    # Comma-separated in .env, e.g. CORS_ORIGINS=https://ops.example.com,https://admin.example.com
    CORS_ORIGINS: Annotated[List[str], NoDecode] = Field(default_factory=list)
    TRUSTED_HOSTS: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "testserver"])
    MAX_BODY_BYTES: int = 1_000_000
    RATE_LIMIT_PER_MIN: int = 240
    WS_MAX_CONNECTIONS_PER_IP: int = 5
    WS_MAX_CONNECTIONS: int = 200

    # ---- Fleet / vehicles (simulated) -------------------------------------
    FLEET_SIZE: int = Field(3, ge=1, le=20)          # robots created on first start
    ROBOT_MAX_SPEED_KMH: float = 25.0
    BATTERY_CAPACITY_KWH: float = 2.5
    BATTERY_CONSUMPTION_KWH_PER_KM: float = 0.08
    CHARGE_RATE_KW: float = Field(1.0, gt=0, le=20)  # at the base station
    LOW_BATTERY_RETURN_PCT: float = Field(25.0, ge=5, le=80)
    MAX_TRIP_KM: float = 15.0
    MAX_LAT_ACCEL: float = 1.5
    MAX_ACCEL: float = 1.0
    MAX_DECEL: float = 2.0
    STOP_DECEL: float = 3.0                          # emergency stop deceleration
    SIM_TIME_SCALE: float = Field(5.0, ge=0.5, le=20.0)  # demo default: 5x real time

    # ---- Operating area (geofence) --------------------------------------
    GEOFENCE_CENTER_LAT: float = 12.9716
    GEOFENCE_CENTER_LON: float = 77.5946
    GEOFENCE_RADIUS_KM: float = 25.0

    # ---- Maps -----------------------------------------------------------
    OFFLINE_MAPS: bool = False  # True = synthetic grid, no network (tests / demos)
    OSM_USER_AGENT: str = "OpenRoad-Autonomy (+https://github.com/Tejascodz/OpenRoad-Autonomy)"
    OSM_CACHE_DIR: Path = BASE_DIR / "data" / "cache"
    OSM_TIMEOUT_S: int = 90  # per request; also the server-side query timeout
    # Download the road map around the geofence centre in the background at startup,
    # so the first route request is instant instead of waiting 30-90 s.
    MAP_WARMUP: bool = True
    MAP_WARMUP_RADIUS_M: float = Field(6000.0, ge=1000.0, le=12000.0)
    NOMINATIM_URL: str = "https://nominatim.openstreetmap.org/search"
    # Overpass servers tried in order (first reachable one wins). The main server is often
    # overloaded or unreachable from some networks, so public mirrors are listed as fallbacks.
    OVERPASS_URLS: Annotated[List[str], NoDecode] = Field(default_factory=lambda: [
        "https://overpass-api.de/api",
        "https://overpass.kumi.systems/api",
        "https://overpass.private.coffee/api",
        "https://maps.mail.ru/osm/tools/overpass/api",
    ])


    # ---- Storage --------------------------------------------------------
    DATABASE_URL: str = f"sqlite:///{(BASE_DIR / 'data' / 'deliveries.db').as_posix()}"
    LOG_DIR: Path = BASE_DIR / "logs"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    _ephemeral_secret: bool = PrivateAttr(False)

    @field_validator("CORS_ORIGINS", "TRUSTED_HOSTS", "OVERPASS_URLS", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator("OVERPASS_URLS")
    @classmethod
    def _https_only(cls, v: List[str]) -> List[str]:
        v = [u.strip().rstrip("/") for u in v if u.strip()]
        if not v or any(not u.startswith("https://") for u in v):
            raise ValueError("OVERPASS_URLS must be a non-empty list of https:// URLs")
        return v

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _no_wildcard_cors(cls, v: List[str]) -> List[str]:
        if any(o.strip() == "*" for o in v):
            raise ValueError("CORS_ORIGINS must be an explicit list; '*' is not allowed with credentials")
        return [o.strip().rstrip("/") for o in v if o.strip()]

    @model_validator(mode="after")
    def _check_security(self) -> "Settings":
        key = self.SECRET_KEY.get_secret_value()
        if self.ENVIRONMENT == "production":
            if self.DEBUG:
                raise ValueError("DEBUG must be false in production")
            if len(key) < 32 or key.lower() in _WEAK_SECRETS:
                raise ValueError("SECRET_KEY must be set to a random value of at least 32 characters in production "
                                 "(run: python scripts/setup_env.py)")
            if not self.COOKIE_SECURE:
                raise ValueError("COOKIE_SECURE must be true in production (serve over HTTPS)")
        elif len(key) < 32:
            # Dev / test convenience on a fresh machine: ephemeral key, sessions reset on restart.
            self.SECRET_KEY = SecretStr(secrets.token_urlsafe(48))
            self._ephemeral_secret = True
        return self

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"



def _setup_logging(s: Settings) -> logging.Logger:
    s.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(s.LOG_DIR / "robot.log", encoding="utf-8"))
    except OSError:  # read-only FS in some containers: stdout only
        pass
    logging.basicConfig(
        level=getattr(logging, s.LOG_LEVEL),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("openroad")


settings = Settings()
logger = _setup_logging(settings)
if settings._ephemeral_secret:
    logger.warning("SECRET_KEY not set - using a temporary random key (logins reset on restart). "
                   "Run `python scripts/setup_env.py` to create a .env.")
