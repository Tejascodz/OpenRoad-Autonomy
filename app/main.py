"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import admin_routes, auth_routes, routes
from .config import BASE_DIR, logger, settings
from .models.delivery import DeliveryStatus
from .models.user import Role
from .security.middleware import (BodySizeLimitMiddleware, GlobalRateLimitMiddleware, OriginCheckMiddleware,
                                  SecurityHeadersMiddleware)
from .security.passwords import hash_password, password_problems
from .services.database_service import get_db
from .services.fleet import home_positions
from .state import broadcaster, fleet, get_map_service

STATIC_DIR = BASE_DIR / "app" / "static"
VERSION = "3.0.0"
_STATUS = {"pickup": DeliveryStatus.PICKUP, "in_transit": DeliveryStatus.IN_TRANSIT,
           "delivered": DeliveryStatus.DELIVERED, "completed": DeliveryStatus.COMPLETED,
           "failed": DeliveryStatus.FAILED, "cancelled": DeliveryStatus.CANCELLED}


def _bootstrap_admin() -> None:
    db = get_db()
    if db.count_users() > 0:
        return
    pw = settings.BOOTSTRAP_ADMIN_PASSWORD.get_secret_value() if settings.BOOTSTRAP_ADMIN_PASSWORD else None
    if not pw:
        logger.warning("No users exist yet. Create an admin with: python -m app.cli create-user --role admin")
        return
    problems = password_problems(pw, settings.BOOTSTRAP_ADMIN_USERNAME)
    if problems:
        logger.error("BOOTSTRAP_ADMIN_PASSWORD rejected: %s", "; ".join(problems))
        return
    db.create_user(settings.BOOTSTRAP_ADMIN_USERNAME, hash_password(pw), Role.ADMIN)
    db.audit("bootstrap.admin_created", actor="system", target=settings.BOOTSTRAP_ADMIN_USERNAME)
    logger.warning("Bootstrap admin '%s' created. Remove BOOTSTRAP_ADMIN_PASSWORD from your .env now.",
                   settings.BOOTSTRAP_ADMIN_USERNAME)


def _load_fleet() -> None:
    db = get_db()
    robots = db.list_robots()
    if not robots:
        center = (settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON)
        for k, home in enumerate(home_positions(settings.FLEET_SIZE, center), start=1):
            db.add_robot(f"R{k:03d}", f"Robot {k}", home[0], home[1])
        robots = db.list_robots()
        logger.info("Created a fleet of %d simulated robots", len(robots))
    for r in robots:
        fleet.add(r["id"], r["name"], (r["home_lat"], r["home_lon"]))


async def _on_mission_status(delivery_id: int, status: str, robot_id: str = "") -> None:
    st = _STATUS.get(status)
    if st:
        await run_in_threadpool(get_db().update_delivery_status, delivery_id, st)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await run_in_threadpool(get_db)
    stale = await run_in_threadpool(db.fail_stale_deliveries)
    if stale:
        logger.info("Marked %d stale deliveries as failed", stale)
    await run_in_threadpool(_bootstrap_admin)
    await run_in_threadpool(_load_fleet)
    fleet.on_status = _on_mission_status
    fleet.planner_for = get_map_service().planner_for
    fleet.start()
    await broadcaster.start()
    if settings.MAP_WARMUP and settings.ENVIRONMENT != "test":
        get_map_service().start_warm_up()
    logger.info("OpenRoad-Autonomy %s started (%s)", VERSION, settings.ENVIRONMENT)
    yield
    await broadcaster.stop()
    await fleet.shutdown()


def create_app() -> FastAPI:
    docs = not settings.is_production
    app = FastAPI(
        title="OpenRoad-Autonomy",
        description="Delivery robot fleet: route planning, dispatch and control API (simulated robots)",
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        debug=False if settings.is_production else settings.DEBUG,
    )

    # ---- middleware (last added = outermost) ----
    if settings.CORS_ORIGINS:
        app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_credentials=True,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"],
                           allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"], max_age=600)
    app.add_middleware(OriginCheckMiddleware, allowed_origins=settings.CORS_ORIGINS)
    app.add_middleware(BodySizeLimitMiddleware, default_limit=settings.MAX_BODY_BYTES,
                       upload_limit=settings.MAX_BODY_BYTES, upload_prefixes=())
    app.add_middleware(GlobalRateLimitMiddleware, per_minute=settings.RATE_LIMIT_PER_MIN)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.TRUSTED_HOSTS)
    app.add_middleware(SecurityHeadersMiddleware)

    # ---- errors: never leak internals ----
    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError):
        errors = [{"loc": [str(x) for x in e.get("loc", ())],
                   "msg": str(e.get("msg", "")).removeprefix("Value error, ")[:200],
                   "type": e.get("type")} for e in exc.errors()[:20]]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(req: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", req.method, req.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    # ---- routes ----
    app.include_router(auth_routes.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(admin_routes.router, prefix="/api/v1/admin", tags=["admin"])
    app.include_router(routes.router, prefix="/api/v1", tags=["robot"])
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    async def ready():
        ok = await run_in_threadpool(get_db().ping)
        return JSONResponse({"status": "ready" if ok else "degraded"}, status_code=200 if ok else 503)

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False,
                log_level=settings.LOG_LEVEL.lower(), server_header=False, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", ws_max_size=65536)
