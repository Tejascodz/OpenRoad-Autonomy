"""Fleet manager: several (simulated) delivery robots.

There is no physical robot connected. Each robot is simulated by following its
planned route with the planned speed profile (acceleration/deceleration limits),
and a battery model. When real robots are connected, replace `Fleet._drive` with
their reported position / speed / battery; everything else (assignment, missions,
events, API, dashboard) stays the same.

Mission = up to two driving legs:  robot position -> pickup  (to_pickup)
                                    pickup -> destination    (to_dropoff)
Return to base = one leg to the robot's base, then it charges there.
"""
from __future__ import annotations

import asyncio
import itertools
import math
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

from ..config import logger, settings
from .battery_model import BatteryModel
from .planning import RoutePlan, RoutePlanner
from .planning.geo import LatLon, haversine
from .planning.trajectory import Trajectory, segment_times

TICK_S = 0.1
MAX_SUBSTEP_S = 0.5
DWELL_S = 30.0          # simulated loading / unloading time
AT_BASE_M = 60.0
MAX_ROUTE_POINTS = 1500

StatusCallback = Callable[[int, str, str], Awaitable[None]]  # delivery_id, status, robot_id
PlannerProvider = Callable[[List[LatLon]], RoutePlanner]


class MissionError(Exception):
    pass


class Mode(str, Enum):
    IDLE = "idle"
    TO_PICKUP = "to_pickup"
    LOADING = "loading"
    TO_DROPOFF = "to_dropoff"
    UNLOADING = "unloading"
    RETURNING = "returning"
    CHARGING = "charging"


DRIVING = {Mode.TO_PICKUP, Mode.TO_DROPOFF, Mode.RETURNING}
LEG_MODE = {"to_pickup": Mode.TO_PICKUP, "to_dropoff": Mode.TO_DROPOFF, "to_base": Mode.RETURNING}


@dataclass
class Leg:
    kind: str                  # to_pickup | to_dropoff | to_base
    traj: Trajectory
    cum_time: np.ndarray       # planned sim seconds to reach each trajectory point
    distance_m: float
    energy_kwh: float
    eta_s: float
    step: int                  # decimation used for the route sent to the dashboard
    summary: dict

    @classmethod
    def from_plan(cls, kind: str, plan: RoutePlan) -> "Leg":
        tr = plan.trajectory
        cum = np.concatenate([[0.0], np.cumsum(segment_times(tr.s, tr.v_ref, settings.MAX_ACCEL))])
        step = max(2, math.ceil(len(tr.points) / MAX_ROUTE_POINTS))
        return cls(kind, tr, cum, plan.distance_m, plan.energy_kwh, float(cum[-1]), step, plan.summary())

    @property
    def length(self) -> float:
        return self.traj.length

    def coords(self) -> List[List[float]]:
        pts = self.traj.points[:: self.step]
        if self.traj.points and pts[-1] != self.traj.points[-1]:
            pts = pts + [self.traj.points[-1]]
        return [[round(a, 6), round(b, 6)] for a, b in pts]

    def point_at(self, s: float) -> LatLon:
        tr = self.traj
        if len(tr.s) < 2:
            return tr.points[0]
        s = min(max(s, 0.0), tr.length)
        j = int(np.searchsorted(tr.s, s))
        j = min(max(j, 1), len(tr.s) - 1)
        s0, s1 = tr.s[j - 1], tr.s[j]
        t = 0.0 if s1 - s0 < 1e-9 else (s - s0) / (s1 - s0)
        (a0, b0), (a1, b1) = tr.points[j - 1], tr.points[j]
        return a0 + (a1 - a0) * t, b0 + (b1 - b0) * t

    def v_ref_at(self, s: float) -> float:
        return float(np.interp(s, self.traj.s, self.traj.v_ref))

    def time_left(self, s: float) -> float:
        return float(self.cum_time[-1] - np.interp(s, self.traj.s, self.cum_time))

    def index_at(self, s: float) -> int:
        return int(np.searchsorted(self.traj.s, s)) // self.step


@dataclass
class Mission:
    delivery_id: Optional[int]
    pickup: Optional[LatLon]
    dropoff: Optional[LatLon]
    legs: List[Leg]
    algorithm: str = "alt"
    profile: str = "fastest"


class Robot:
    def __init__(self, rid: str, name: str, home: LatLon):
        self.id = rid
        self.name = name
        self.home = home
        self.lat, self.lon = home
        self.mode = Mode.IDLE
        self.battery = BatteryModel()
        self.v = 0.0
        self.s = 0.0
        self.dwell = 0.0
        self.mission: Optional[Mission] = None
        self.stopped = False
        self.error: Optional[str] = None
        self.route_version = 0
        self.odometer_m = 0.0

    @property
    def leg(self) -> Optional[Leg]:
        return self.mission.legs[0] if self.mission and self.mission.legs else None

    @property
    def position(self) -> LatLon:
        return self.lat, self.lon

    @property
    def at_base(self) -> bool:
        return haversine(self.position, self.home) <= AT_BASE_M

    @property
    def available(self) -> bool:
        return not self.stopped and self.mission is None and self.mode in (Mode.IDLE, Mode.CHARGING)

    def eta_s(self) -> float:
        """Remaining simulated seconds for the whole mission."""
        if not self.mission:
            return 0.0
        total = 0.0
        for i, leg in enumerate(self.mission.legs):
            total += leg.time_left(self.s) if i == 0 and self.mode in DRIVING else leg.eta_s
        if self.mission.delivery_id is not None:
            kinds = [lg.kind for lg in self.mission.legs]
            if self.mode == Mode.LOADING:
                total += max(0.0, DWELL_S - self.dwell) + DWELL_S
            elif self.mode == Mode.UNLOADING:
                total += max(0.0, DWELL_S - self.dwell)
            else:
                total += DWELL_S * (("to_pickup" in kinds) + ("to_dropoff" in kinds))
        return total

    def remaining_m(self) -> float:
        if not self.mission:
            return 0.0
        rem = 0.0
        for i, leg in enumerate(self.mission.legs):
            rem += leg.length - self.s if i == 0 and self.mode in DRIVING else leg.length
        return max(0.0, rem)

    def snapshot(self, time_scale: float) -> dict:
        leg = self.leg
        m = self.mission
        return {
            "id": self.id, "name": self.name, "mode": self.mode.value, "stopped": self.stopped,
            "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "speed_kmh": round(self.v * 3.6, 1),
            "battery_pct": round(self.battery.get_percentage(), 1),
            "range_km": round(self.battery.estimate_range() / 1000, 1),
            "delivery_id": m.delivery_id if m else None,
            "leg": leg.kind if leg else None,
            "leg_progress": round(100 * self.s / leg.length, 1) if leg and leg.length and self.mode in DRIVING else
            (100.0 if self.mode in (Mode.LOADING, Mode.UNLOADING) else 0.0),
            "route_point": leg.index_at(self.s) if leg and self.mode in DRIVING else 0,
            "remaining_m": round(self.remaining_m()),
            "eta_s": round(self.eta_s() / max(time_scale, 1e-6), 1),
            "route_version": self.route_version,
            "home": [round(self.home[0], 6), round(self.home[1], 6)],
            "at_base": self.at_base,
            "odometer_km": round(self.odometer_m / 1000, 2),
            "error": self.error,
        }


class Fleet:
    def __init__(self) -> None:
        self.robots: Dict[str, Robot] = {}
        self.time_scale = settings.SIM_TIME_SCALE
        self.events: Deque[dict] = deque(maxlen=200)
        self._event_ids = itertools.count(1)
        self.on_status: Optional[StatusCallback] = None
        self.planner_for: Optional[PlannerProvider] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._bg: set = set()

    # ------------------------------------------------------------------ registry
    def add(self, rid: str, name: str, home: LatLon) -> Robot:
        r = Robot(rid, name, home)
        self.robots[rid] = r
        return r

    def remove(self, rid: str) -> None:
        r = self.get(rid)
        if not r.available:
            raise MissionError(f"{rid} is busy - cancel its mission first")
        del self.robots[rid]

    def get(self, rid: str) -> Robot:
        r = self.robots.get(rid)
        if r is None:
            raise KeyError(rid)
        return r

    def log(self, level: str, message: str, robot_id: Optional[str] = None) -> None:
        self.events.append({"id": next(self._event_ids), "ts": time.time(), "level": level,
                            "robot": robot_id, "message": message})

    def set_time_scale(self, scale: float) -> float:
        self.time_scale = max(0.5, min(20.0, float(scale)))
        self.log("info", f"Simulation speed set to {self.time_scale:g}x")
        return self.time_scale

    def snapshot(self) -> dict:
        return {"time_scale": self.time_scale, "simulated": True,
                "robots": [r.snapshot(self.time_scale) for r in sorted(self.robots.values(), key=lambda r: r.id)],
                "events": list(self.events)[-30:]}

    def route(self, rid: str) -> dict:
        r = self.get(rid)
        m = r.mission
        return {"robot": rid, "version": r.route_version,
                "legs": [{"kind": lg.kind, "coords": lg.coords()} for lg in (m.legs if m else [])],
                "pickup": list(m.pickup) if m and m.pickup else None,
                "dropoff": list(m.dropoff) if m and m.dropoff else None}

    # ------------------------------------------------------------------ planning (blocking: run in a thread)
    @staticmethod
    def plan_leg(planner: RoutePlanner, start: LatLon, end: LatLon, kind: str,
                 algorithm: str, profile: str) -> Optional[Leg]:
        from .map_service import MAX_SNAP_DISTANCE_M
        s_node, ds = planner.snap(*start)
        t_node, dt = planner.snap(*end)
        if ds > MAX_SNAP_DISTANCE_M or dt > MAX_SNAP_DISTANCE_M:
            raise ValueError("location is too far from any road")
        if s_node == t_node:
            return None
        return Leg.from_plan(kind, planner.plan(s_node, t_node, algorithm, profile))

    def plan_delivery(self, planner: RoutePlanner, pickup: LatLon, dropoff: LatLon, algorithm: str,
                      profile: str, robot_id: Optional[str]) -> Tuple[Robot, List[Leg]]:
        """Choose a robot (requested one, or the available robot that reaches the pickup first)
        and plan its legs. Blocking."""
        delivery = self.plan_leg(planner, pickup, dropoff, "to_dropoff", algorithm, profile)
        if delivery is None:
            raise ValueError("pickup and destination are on the same road point")
        if robot_id:
            r = self.robots.get(robot_id)
            if r is None:
                raise MissionError(f"unknown robot {robot_id}")
            candidates = [r]
        else:
            candidates = [r for r in self.robots.values() if r.available]
        if not candidates:
            raise MissionError("no robot is available right now - all are busy or stopped")
        options, reasons = [], []
        for r in candidates:
            if not r.available:
                reasons.append(f"{r.id} is busy" if not r.stopped else f"{r.id} is emergency-stopped")
                continue
            approach = self.plan_leg(planner, r.position, pickup, "to_pickup", "alt", profile)
            need = delivery.energy_kwh + (approach.energy_kwh if approach else 0.0)
            if not r.battery.can_complete_energy(need):
                reasons.append(f"{r.id} battery too low ({r.battery.get_percentage():.0f}%)")
                continue
            options.append(((approach.eta_s if approach else 0.0), r.id, r, approach))
        if not options:
            raise MissionError("; ".join(reasons) or "no suitable robot")
        _eta, _id, robot, approach = min(options, key=lambda o: (o[0], o[1]))
        return robot, ([approach] if approach else []) + [delivery]

    # ------------------------------------------------------------------ actions
    async def assign(self, robot: Robot, delivery_id: int, pickup: LatLon, dropoff: LatLon, legs: List[Leg],
                     algorithm: str, profile: str) -> None:
        async with self._lock:
            if robot.id not in self.robots or not robot.available:
                raise MissionError(f"{robot.id} just became busy - please try again")
            robot.battery.stats.charging = False
            robot.mission = Mission(delivery_id, pickup, dropoff, list(legs), algorithm, profile)
            robot.s = 0.0
            robot.dwell = 0.0
            robot.error = None
            robot.mode = LEG_MODE[legs[0].kind] if legs[0].kind == "to_pickup" else Mode.LOADING
            robot.route_version += 1
            total = sum(lg.distance_m for lg in legs)
            self.log("success", f"Delivery #{delivery_id} assigned to {robot.id} ({total / 1000:.2f} km incl. "
                                f"approach)", robot.id)
        if robot.mode == Mode.LOADING:
            await self._emit(delivery_id, "pickup", robot.id)

    async def return_to_base(self, rid: str, auto: bool = False) -> None:
        r = self.get(rid)
        if not r.available:
            raise MissionError(f"{rid} is busy")
        if r.at_base:
            raise MissionError(f"{rid} is already at its base")
        if self.planner_for is None:
            raise MissionError("road map not available")
        pos, home = r.position, r.home
        planner_for = self.planner_for

        def work():
            planner = planner_for([pos, home])
            return self.plan_leg(planner, pos, home, "to_base", "alt", "fastest")

        leg = await asyncio.to_thread(work)
        async with self._lock:
            if not r.available:
                raise MissionError(f"{rid} just became busy")
            if leg is None:
                return
            r.battery.stats.charging = False
            r.mission = Mission(None, None, None, [leg])
            r.s = 0.0
            r.mode = Mode.RETURNING
            r.route_version += 1
            self.log("info", f"{rid} returning to base" + (" (battery low)" if auto else ""), rid)

    def stop(self, rid: str) -> None:
        r = self.get(rid)
        if not r.stopped:
            r.stopped = True
            self.log("critical", f"EMERGENCY STOP: {rid}", rid)

    def stop_all(self) -> int:
        n = 0
        for rid in list(self.robots):
            if not self.robots[rid].stopped:
                self.stop(rid)
                n += 1
        return n

    def resume(self, rid: str) -> None:
        r = self.get(rid)
        if r.stopped:
            r.stopped = False
            if r.error and "battery" not in r.error:
                r.error = None
            self.log("info", f"{rid} released from emergency stop", rid)

    def resume_all(self) -> int:
        n = 0
        for r in self.robots.values():
            if r.stopped:
                self.resume(r.id)
                n += 1
        return n

    async def cancel(self, rid: str, reason: str = "cancelled by operator") -> Optional[int]:
        r = self.get(rid)
        async with self._lock:
            if r.mission is None:
                raise MissionError(f"{rid} has no active mission")
            did = r.mission.delivery_id
            r.mission = None
            r.mode = Mode.IDLE
            r.route_version += 1
            self.log("warning", f"{rid}: mission cancelled ({reason})", rid)
        if did is not None:
            await self._emit(did, "cancelled", rid)
        return did

    # ------------------------------------------------------------------ simulation loop
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="fleet-loop")

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        last = time.monotonic()
        while True:
            try:
                await asyncio.sleep(TICK_S)
                now = time.monotonic()
                dt = min(now - last, 0.5) * self.time_scale
                last = now
                await self.tick(dt)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fleet tick failed")

    async def tick(self, dt: float) -> None:
        for r in list(self.robots.values()):
            await self._step(r, dt)

    async def _step(self, r: Robot, dt: float) -> None:
        if r.stopped:
            if r.v > 0 and r.leg is not None:
                self._advance(r, r.leg, -settings.STOP_DECEL, dt)
            r.v = 0.0 if r.leg is None else r.v
            return
        if r.mode in DRIVING:
            remaining = dt
            while remaining > 1e-9 and r.mode in DRIVING and r.leg is not None and not r.stopped:
                h = min(MAX_SUBSTEP_S, remaining)
                done = self._drive(r, r.leg, h)
                remaining -= h
                if done:
                    await self._leg_finished(r)
        elif r.mode == Mode.LOADING:
            r.dwell += dt
            if r.dwell >= DWELL_S:
                r.dwell = 0.0
                r.s = 0.0
                r.mode = Mode.TO_DROPOFF
                self.log("info", f"{r.id}: parcel loaded for delivery #{r.mission.delivery_id}, departing", r.id)
                await self._emit(r.mission.delivery_id, "in_transit", r.id)
        elif r.mode == Mode.UNLOADING:
            r.dwell += dt
            if r.dwell >= DWELL_S:
                did = r.mission.delivery_id
                r.mission = None
                r.mode = Mode.IDLE
                r.route_version += 1
                self.log("success", f"{r.id}: delivery #{did} completed", r.id)
                await self._emit(did, "completed", r.id)
                if r.battery.get_percentage() < settings.LOW_BATTERY_RETURN_PCT and not r.at_base:
                    self._background(self._auto_return(r.id))
        elif r.mode == Mode.CHARGING:
            if not r.at_base:
                r.mode = Mode.IDLE
                return
            r.battery.charge(settings.CHARGE_RATE_KW * dt / 3600.0)
            if r.battery.get_percentage() >= 99.9:
                r.battery.stats.charging = False
                r.mode = Mode.IDLE
                self.log("success", f"{r.id} fully charged", r.id)
        elif r.mode == Mode.IDLE and r.at_base and r.battery.get_percentage() < 99.5:
            r.mode = Mode.CHARGING

    def _drive(self, r: Robot, leg: Leg, dt: float) -> bool:
        """Follow the planned speed profile along the leg. Returns True when the leg is finished."""
        remaining = leg.length - r.s
        v_target = leg.v_ref_at(min(leg.length, r.s + r.v * dt))
        if remaining > 0.3:
            v_target = max(v_target, 0.3)
        accel = max(-settings.MAX_DECEL, min(settings.MAX_ACCEL, (v_target - r.v) / 0.5))
        self._advance(r, leg, accel, dt)
        return leg.length - r.s < 0.3 and r.v < 0.6

    def _advance(self, r: Robot, leg: Leg, accel: float, dt: float) -> None:
        v0 = r.v
        r.v = max(0.0, r.v + accel * dt)
        ds = min(max(0.0, (v0 + r.v) / 2 * dt), leg.length - r.s)
        if ds > 0:
            res = r.battery.consume_energy(ds, max(r.v, 0.1) * 3.6)
            if not res["success"]:
                r.error = "battery empty"
                self.stop(r.id)
                return
            r.s += ds
            r.odometer_m += ds
        r.lat, r.lon = leg.point_at(r.s)

    async def _leg_finished(self, r: Robot) -> None:
        leg = r.mission.legs.pop(0)
        r.route_version += 1  # the dashboard re-fetches the remaining legs
        r.v = 0.0
        r.s = 0.0
        r.lat, r.lon = leg.point_at(leg.length)
        m = r.mission
        if leg.kind == "to_pickup":
            r.mode = Mode.LOADING
            r.dwell = 0.0
            self.log("info", f"{r.id} arrived at pickup for delivery #{m.delivery_id}", r.id)
            await self._emit(m.delivery_id, "pickup", r.id)
        elif leg.kind == "to_dropoff":
            r.mode = Mode.UNLOADING
            r.dwell = 0.0
            self.log("success", f"{r.id} arrived at destination of delivery #{m.delivery_id}", r.id)
            await self._emit(m.delivery_id, "delivered", r.id)
        else:  # to_base
            r.mission = None
            r.mode = Mode.CHARGING if r.battery.get_percentage() < 99.5 else Mode.IDLE
            r.route_version += 1
            self.log("info", f"{r.id} is back at base" + (" and charging" if r.mode == Mode.CHARGING else ""), r.id)

    async def _auto_return(self, rid: str) -> None:
        try:
            await self.return_to_base(rid, auto=True)
        except (MissionError, ValueError, KeyError) as exc:
            logger.info("auto return for %s skipped: %s", rid, exc)
        except Exception:
            logger.exception("auto return for %s failed", rid)

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _emit(self, delivery_id: Optional[int], status: str, rid: str) -> None:
        if delivery_id is None or not self.on_status:
            return
        try:
            await self.on_status(delivery_id, status, rid)
        except Exception:
            logger.exception("status callback failed")


def home_positions(n: int, center: LatLon, radius_m: float = 80.0) -> List[LatLon]:
    """Spread robot bases in a small ring around the depot so markers don't overlap."""
    out = []
    for k in range(n):
        ang = 2 * math.pi * k / max(n, 1)
        dlat = radius_m * math.cos(ang) / 111_195.0
        dlon = radius_m * math.sin(ang) / (111_195.0 * math.cos(math.radians(center[0])))
        out.append((center[0] + dlat, center[1] + dlon))
    return out
