"""Planning, fleet, delivery and map endpoints (all require authentication)."""
from __future__ import annotations

import asyncio
import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, WebSocket, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from ..config import logger, settings
from ..models.delivery import DeliveryStatus
from ..security.deps import Principal, load_principal, require_admin, require_operator, require_viewer
from ..security.middleware import origin_allowed
from ..security.rate_limit import RateLimit, client_ip, limiter
from ..security.tokens import ACCESS_COOKIE
from ..services.database_service import get_db
from ..services.fleet import MissionError
from ..services.geocode import GeocodeUnavailable, clean_query
from ..services.geocode import search as geocode_search
from ..services.map_service import MapDataUnavailable
from ..services.planning import ALGORITHMS, PROFILES, NoRouteError
from ..services.planning.geo import haversine, inside_geofence
from ..state import fleet, get_map_service, manager

router = APIRouter()

Lat = Field(..., ge=-90, le=90, allow_inf_nan=False)
Lon = Field(..., ge=-180, le=180, allow_inf_nan=False)
Algorithm = Literal["alt", "astar", "dijkstra", "dstar_lite"]
Profile = Literal["fastest", "shortest", "energy", "safest"]
ROBOT_ID_RE = r"^R\d{3}$"
RobotId = Path(..., pattern=ROBOT_ID_RE)
if set(Algorithm.__args__) != set(ALGORITHMS) or set(Profile.__args__) != set(PROFILES):
    raise RuntimeError("API enums out of sync with planner")


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pickup_lat: float = Lat
    pickup_lon: float = Lon
    delivery_lat: float = Lat
    delivery_lon: float = Lon
    algorithm: Algorithm = "alt"
    profile: Profile = "fastest"
    alternatives: int = Field(0, ge=0, le=3)
    robot_id: Optional[str] = Field(None, pattern=ROBOT_ID_RE, description="omit = nearest available robot")

    @model_validator(mode="after")
    def _check(self) -> "RouteRequest":
        a, b = (self.pickup_lat, self.pickup_lon), (self.delivery_lat, self.delivery_lon)
        d = haversine(a, b)
        if d < 20:
            raise ValueError("pickup and delivery must be at least 20 m apart")
        if d > settings.MAX_TRIP_KM * 1000:
            raise ValueError(f"trip longer than MAX_TRIP_KM ({settings.MAX_TRIP_KM} km)")
        c = (settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON)
        for p, name in ((a, "pickup"), (b, "delivery")):
            if not inside_geofence(p, c, settings.GEOFENCE_RADIUS_KM):
                raise ValueError(f"{name} is outside the operating area (geofence)")
        return self


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, MapDataUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, NoRouteError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No drivable route between these points")
    if isinstance(exc, MissionError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, ValueError):  # our own validation messages (safe to show)
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    raise exc


def _plan_blocking(req: RouteRequest, alternatives: int):
    a, b = (req.pickup_lat, req.pickup_lon), (req.delivery_lat, req.delivery_lon)
    ms = get_map_service()
    planner = ms.planner_for([a, b])
    s = ms.snap_or_raise(planner, a, "pickup")
    t = ms.snap_or_raise(planner, b, "delivery")
    plan = planner.plan(s, t, req.algorithm, req.profile, alternatives)
    try:
        robot, legs = fleet.plan_delivery(planner, a, b, req.algorithm, req.profile, req.robot_id)
        approach = next((lg for lg in legs if lg.kind == "to_pickup"), None)
        suggestion = {"robot_id": robot.id, "approach_m": round(approach.distance_m) if approach else 0,
                      "approach_min": round(approach.eta_s / 60, 1) if approach else 0.0}
    except MissionError as exc:
        suggestion = {"robot_id": None, "reason": str(exc)}
    return plan, suggestion


def _coords(points, limit: int = 1500):
    step = max(1, len(points) // limit)
    pts = points[::step] + [points[-1]]
    return [[round(a, 6), round(b, 6)] for a, b in pts]


# ---------------------------------------------------------------- planning & dispatch
@router.post("/plan", dependencies=[Depends(RateLimit("plan", 30, 60))])
async def plan_route(req: RouteRequest, p: Principal = Depends(require_operator)):
    """Preview the delivery route (+ alternatives) and which robot would take it."""
    try:
        plan, suggestion = await run_in_threadpool(_plan_blocking, req, req.alternatives)
    except Exception as exc:
        raise _errors(exc)
    return {**plan.summary(), "coords": _coords(plan.trajectory.points), "alternatives": plan.alternatives,
            "robot": suggestion}


@router.post("/start_delivery", dependencies=[Depends(RateLimit("start_delivery", 20, 60))])
async def start_delivery(req: RouteRequest, request: Request, p: Principal = Depends(require_operator)):
    a, b = (req.pickup_lat, req.pickup_lon), (req.delivery_lat, req.delivery_lon)

    def work():
        planner = get_map_service().planner_for([a, b])
        return fleet.plan_delivery(planner, a, b, req.algorithm, req.profile, req.robot_id)

    try:
        robot, legs = await run_in_threadpool(work)
    except Exception as exc:
        raise _errors(exc)
    delivery_leg = legs[-1]
    db = get_db()
    rec = await run_in_threadpool(
        db.create_delivery, a[0], a[1], b[0], b[1], delivery_leg.traj.points, delivery_leg.distance_m,
        delivery_leg.eta_s / 60.0, algorithm=req.algorithm, profile=req.profile,
        energy_kwh=delivery_leg.energy_kwh, created_by=p.user.id, robot_id=robot.id)
    did = rec["id"]
    try:
        await fleet.assign(robot, did, a, b, legs, req.algorithm, req.profile)
    except MissionError as exc:
        await run_in_threadpool(db.update_delivery_status, did, DeliveryStatus.FAILED)
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    await run_in_threadpool(db.update_delivery_status, did, DeliveryStatus.PICKUP)
    await run_in_threadpool(db.audit, "delivery.start", actor=p.username, target=str(did), ip=client_ip(request),
                            detail={"robot": robot.id, "algorithm": req.algorithm, "profile": req.profile})
    approach = legs[0] if legs[0].kind == "to_pickup" else None
    return {"success": True, "delivery_id": did, "robot_id": robot.id,
            "distance_m": round(delivery_leg.distance_m, 1),
            "approach_m": round(approach.distance_m, 1) if approach else 0.0,
            "eta_min": round(robot.eta_s() / 60.0, 2),
            "message": f"Delivery #{did} assigned to {robot.id}"}


# ---------------------------------------------------------------- fleet
@router.get("/fleet")
async def get_fleet(p: Principal = Depends(require_viewer)):
    return fleet.snapshot()


def _robot_or_404(rid: str):
    try:
        return fleet.get(rid)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Robot not found")


@router.get("/robots/{rid}/route")
async def robot_route(rid: str = RobotId, p: Principal = Depends(require_viewer)):
    _robot_or_404(rid)
    return fleet.route(rid)


@router.post("/robots/{rid}/stop")
async def robot_stop(request: Request, rid: str = RobotId, p: Principal = Depends(require_viewer)):
    """Any signed-in user may stop a robot (safety first); only operators can release it."""
    _robot_or_404(rid)
    fleet.stop(rid)
    await run_in_threadpool(get_db().audit, "robot.stop", actor=p.username, target=rid, ip=client_ip(request))
    return {"success": True, "message": f"{rid} stopped"}


@router.post("/robots/{rid}/resume")
async def robot_resume(request: Request, rid: str = RobotId, p: Principal = Depends(require_operator)):
    _robot_or_404(rid)
    fleet.resume(rid)
    await run_in_threadpool(get_db().audit, "robot.resume", actor=p.username, target=rid, ip=client_ip(request))
    return {"success": True, "message": f"{rid} released"}


@router.post("/robots/{rid}/cancel")
async def robot_cancel(request: Request, rid: str = RobotId, p: Principal = Depends(require_operator)):
    _robot_or_404(rid)
    try:
        did = await fleet.cancel(rid)
    except MissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    await run_in_threadpool(get_db().audit, "robot.cancel", actor=p.username, target=rid, ip=client_ip(request),
                            detail={"delivery": did})
    return {"success": True, "message": f"{rid}: mission cancelled"}


@router.post("/robots/{rid}/return")
async def robot_return(request: Request, rid: str = RobotId, p: Principal = Depends(require_operator)):
    _robot_or_404(rid)
    try:
        await fleet.return_to_base(rid)
    except Exception as exc:
        raise _errors(exc)
    await run_in_threadpool(get_db().audit, "robot.return", actor=p.username, target=rid, ip=client_ip(request))
    return {"success": True, "message": f"{rid} returning to base"}


@router.post("/fleet/stop_all")
async def stop_all(request: Request, p: Principal = Depends(require_viewer)):
    n = fleet.stop_all()
    await run_in_threadpool(get_db().audit, "fleet.stop_all", actor=p.username, ip=client_ip(request))
    return {"success": True, "message": f"Emergency stop: {n} robot(s) stopped"}


@router.post("/fleet/resume_all")
async def resume_all(request: Request, p: Principal = Depends(require_operator)):
    n = fleet.resume_all()
    await run_in_threadpool(get_db().audit, "fleet.resume_all", actor=p.username, ip=client_ip(request))
    return {"success": True, "message": f"{n} robot(s) released"}


class SimSpeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scale: float = Field(..., ge=0.5, le=20, allow_inf_nan=False)


@router.post("/fleet/sim_speed")
async def set_sim_speed(body: SimSpeed, p: Principal = Depends(require_operator)):
    return {"time_scale": fleet.set_time_scale(body.scale)}


# ---------------------------------------------------------------- deliveries
@router.get("/deliveries/active")
def get_active_deliveries(p: Principal = Depends(require_viewer)):
    return get_db().get_active_deliveries()


@router.get("/deliveries/history")
def get_delivery_history(limit: int = Query(20, ge=1, le=100), p: Principal = Depends(require_viewer)):
    return get_db().get_delivery_history(limit)


@router.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: int = Path(..., ge=1, le=2**31 - 1), p: Principal = Depends(require_viewer)):
    d = get_db().get_delivery(delivery_id)
    if not d:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Delivery not found")
    return d


# ---------------------------------------------------------------- map
@router.get("/map/status")
def map_status(p: Principal = Depends(require_viewer)):
    return {**get_map_service().status(), "offline": settings.OFFLINE_MAPS,
            "geofence": {"lat": settings.GEOFENCE_CENTER_LAT, "lon": settings.GEOFENCE_CENTER_LON,
                         "radius_km": settings.GEOFENCE_RADIUS_KM}}


class OfflineToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.post("/map/offline")
async def set_offline_maps(body: OfflineToggle, request: Request, p: Principal = Depends(require_operator)):
    """Fallback when OpenStreetMap servers are unreachable: use the synthetic demo road grid."""
    if any(r.mission for r in fleet.robots.values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "Finish or cancel all active missions first")
    await run_in_threadpool(get_map_service().set_offline, body.enabled)
    await run_in_threadpool(get_db().audit, "map.offline", actor=p.username, ip=client_ip(request),
                            detail={"enabled": body.enabled})
    return get_map_service().status()


@router.post("/map/retry")
async def retry_map_download(request: Request, p: Principal = Depends(require_operator)):
    """Retry the background road-map download now (e.g. after the OSM servers were busy)."""
    started = get_map_service().start_warm_up()
    await run_in_threadpool(get_db().audit, "map.retry", actor=p.username, ip=client_ip(request))
    return {"started": started, **get_map_service().status()}


@router.get("/geocode", dependencies=[Depends(RateLimit("geocode", 40, 60))])
def geocode(q: str = Query(..., min_length=3, max_length=120), p: Principal = Depends(require_operator)):
    """Search places inside the loaded road map (OpenStreetMap Nominatim)."""
    if settings.OFFLINE_MAPS:
        return []
    try:
        results = geocode_search(clean_query(q))
    except GeocodeUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Place search is unavailable right now")
    ms = get_map_service()
    if ms.operating_area() is None:
        return results
    return [r for r in results if ms.in_area((r["lat"], r["lon"]))]


@router.get("/system/info")
def system_info(p: Principal = Depends(require_admin)):
    """Non-secret runtime configuration for administrators."""
    return {"environment": settings.ENVIRONMENT, "offline_maps": settings.OFFLINE_MAPS,
            "geofence_km": settings.GEOFENCE_RADIUS_KM, "max_trip_km": settings.MAX_TRIP_KM,
            "sim_time_scale": fleet.time_scale, "robots": len(fleet.robots), "ws_connections": manager.size,
            "algorithms": list(ALGORITHMS), "profiles": list(PROFILES)}


# ---------------------------------------------------------------- websocket
@router.websocket("/ws/fleet")
async def websocket_endpoint(websocket: WebSocket):
    ip = websocket.client.host if websocket.client else "unknown"
    origin = websocket.headers.get("origin")
    allowed = {o.lower() for o in settings.CORS_ORIGINS}
    # Cross-site WebSocket hijacking protection: browsers always send Origin.
    if not origin or not origin_allowed(origin, websocket.headers.get("host", ""), allowed):
        await websocket.close(code=1008)
        return
    if limiter.hit(f"ws:{ip}", 20, 60) is not None:
        await websocket.close(code=1008)
        return
    token = websocket.cookies.get(ACCESS_COOKIE)
    principal = await run_in_threadpool(load_principal, token, True) if token and len(token) < 4096 else None
    if principal is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    if not await manager.register(websocket, ip, principal.username, float(principal.claims["exp"])):
        await websocket.close(code=1013)  # try again later
        return
    try:
        await websocket.send_text(json.dumps({"type": "fleet", "data": fleet.snapshot()}, default=str))
        while True:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=120)
            if len(raw) > 1024 or limiter.hit(f"wsmsg:{ip}", 60, 60) is not None:
                await websocket.close(code=1008)
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await websocket.send_text('{"type":"pong"}')
    except Exception as exc:  # disconnects, timeouts
        logger.debug("WebSocket ended: %s", type(exc).__name__)
    finally:
        await manager.unregister(websocket)
