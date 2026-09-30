"""Fleet simulation: assignment, missions, stop/cancel, return-to-base and charging."""
import asyncio

import pytest

from app.config import settings
from app.services.fleet import DWELL_S, Fleet, MissionError, Mode, home_positions
from app.services.planning import RoutePlanner, VehicleLimits
from app.services.planning.graph import synthetic_grid_graph

C = (12.97, 77.59)


@pytest.fixture(scope="module")
def planner():
    return RoutePlanner(synthetic_grid_graph(C[0], C[1], size=31, spacing_m=100), VehicleLimits())


def make_fleet(planner, n=3):
    f = Fleet()
    for k, home in enumerate(home_positions(n, C), start=1):
        f.add(f"R{k:03d}", f"Robot {k}", home)
    f.planner_for = lambda pts: planner
    events = []

    async def cb(did, status, rid):
        events.append((did, status, rid))

    f.on_status = cb
    return f, events


async def run_until(f, cond, dt=1.0, limit=20000, pause=0.0):
    for _ in range(limit):
        if cond():
            return
        await f.tick(dt)
        await asyncio.sleep(pause)  # let background tasks (e.g. auto return planning) run
    raise AssertionError("condition not reached")


def test_nearest_available_robot_is_chosen(planner):
    f, _ = make_fleet(planner)
    f.get("R002").lat, f.get("R002").lon = 12.985, 77.605   # move R002 close to the pickup
    robot, legs = f.plan_delivery(planner, (12.984, 77.604), (12.96, 77.58), "alt", "fastest", None)
    assert robot.id == "R002"
    assert [lg.kind for lg in legs][-1] == "to_dropoff"


def test_full_delivery_lifecycle_and_statuses(planner):
    async def go():
        f, events = make_fleet(planner)
        pickup, drop = (12.975, 77.595), (12.962, 77.583)
        robot, legs = f.plan_delivery(planner, pickup, drop, "alt", "fastest", None)
        await f.assign(robot, 1, pickup, drop, legs, "alt", "fastest")
        assert robot.mode in (Mode.TO_PICKUP, Mode.LOADING)
        assert not robot.available
        await run_until(f, lambda: robot.mission is None)
        statuses = [s for d, s, r in events if d == 1]
        assert statuses[-3:] == ["in_transit", "delivered", "completed"] and "pickup" in statuses
        assert robot.mode in (Mode.IDLE, Mode.CHARGING, Mode.RETURNING)
        assert abs(robot.lat - drop[0]) < 0.003 and abs(robot.lon - drop[1]) < 0.003  # ended at the destination
        assert robot.battery.get_percentage() < 100 and robot.odometer_m > 1000
    asyncio.run(go())


def test_parallel_missions_on_different_robots(planner):
    async def go():
        f, events = make_fleet(planner)
        jobs = [((12.975, 77.595), (12.962, 77.583)), ((12.966, 77.600), (12.978, 77.582)),
                ((12.958, 77.590), (12.980, 77.600))]
        used = set()
        for i, (a, b) in enumerate(jobs, start=1):
            robot, legs = f.plan_delivery(planner, a, b, "alt", "fastest", None)
            await f.assign(robot, i, a, b, legs, "alt", "fastest")
            used.add(robot.id)
        assert used == {"R001", "R002", "R003"}
        with pytest.raises(MissionError, match="no robot is available"):
            f.plan_delivery(planner, jobs[0][0], jobs[0][1], "alt", "fastest", None)
        await run_until(f, lambda: all(r.mission is None for r in f.robots.values()))
        assert sorted(d for d, s, r in events if s == "completed") == [1, 2, 3]
    asyncio.run(go())


def test_emergency_stop_holds_robot_and_resume_continues(planner):
    async def go():
        f, _ = make_fleet(planner, n=1)
        a, b = (12.975, 77.595), (12.962, 77.583)
        robot, legs = f.plan_delivery(planner, a, b, "alt", "fastest", None)
        await f.assign(robot, 7, a, b, legs, "alt", "fastest")
        await run_until(f, lambda: robot.v > 3.0, dt=0.2)
        f.stop_all()
        for _ in range(30):
            await f.tick(0.2)
        pos = robot.position
        assert robot.v == 0.0 and robot.stopped
        for _ in range(50):
            await f.tick(1.0)
        assert robot.position == pos and not robot.available
        with pytest.raises(MissionError):
            f.plan_delivery(planner, a, b, "alt", "fastest", "R001")
        f.resume("R001")
        await run_until(f, lambda: robot.mission is None)
    asyncio.run(go())


def test_cancel_marks_failed_and_frees_robot(planner):
    async def go():
        f, events = make_fleet(planner, n=1)
        a, b = (12.975, 77.595), (12.962, 77.583)
        robot, legs = f.plan_delivery(planner, a, b, "alt", "fastest", None)
        await f.assign(robot, 9, a, b, legs, "alt", "fastest")
        await f.tick(5.0)
        assert await f.cancel("R001") == 9
        assert ("9", "cancelled") in {(str(d), s) for d, s, r in events} and robot.available
        with pytest.raises(MissionError):
            await f.cancel("R001")
    asyncio.run(go())


def test_low_battery_is_refused_and_robot_returns_and_charges(planner, monkeypatch):
    async def go():
        f, _ = make_fleet(planner, n=2)
        low = f.get("R001")
        low.battery.stats.current_charge_kwh = 0.27  # ~ reserve only
        a, b = (12.975, 77.595), (12.962, 77.583)
        robot, _ = f.plan_delivery(planner, a, b, "alt", "fastest", None)
        assert robot.id == "R002"
        with pytest.raises(MissionError, match="battery too low"):
            f.plan_delivery(planner, a, b, "alt", "fastest", "R001")
        low.lat, low.lon = 12.98, 77.60            # away from base
        low.mode = Mode.IDLE
        await f.return_to_base("R001")
        assert low.mode == Mode.RETURNING
        await run_until(f, lambda: low.mode == Mode.CHARGING)
        assert low.at_base
        monkeypatch.setattr(settings, "CHARGE_RATE_KW", 20.0)
        await run_until(f, lambda: low.mode == Mode.IDLE, dt=5.0)
        assert low.battery.get_percentage() >= 99.9
    asyncio.run(go())


def test_auto_return_after_delivery_when_battery_low(planner):
    async def go():
        f, _ = make_fleet(planner, n=1)
        r = f.get("R001")
        a, b = (12.975, 77.595), (12.962, 77.583)
        robot, legs = f.plan_delivery(planner, a, b, "alt", "fastest", None)
        await f.assign(robot, 3, a, b, legs, "alt", "fastest")
        await run_until(f, lambda: r.mode == Mode.UNLOADING)
        r.battery.stats.current_charge_kwh = 0.4  # 16 % < LOW_BATTERY_RETURN_PCT
        await run_until(f, lambda: r.mode == Mode.RETURNING, pause=0.01, limit=2000)
        await run_until(f, lambda: r.mode in (Mode.CHARGING, Mode.IDLE) and r.mission is None)
        assert r.at_base
    asyncio.run(go())


def test_eta_counts_down(planner):
    async def go():
        f, _ = make_fleet(planner, n=1)
        a, b = (12.975, 77.595), (12.962, 77.583)
        robot, legs = f.plan_delivery(planner, a, b, "alt", "fastest", None)
        await f.assign(robot, 4, a, b, legs, "alt", "fastest")
        e0 = robot.eta_s()
        assert e0 > 2 * DWELL_S
        for _ in range(60):
            await f.tick(1.0)
        assert robot.eta_s() < e0 - 30
        snap = robot.snapshot(f.time_scale)
        assert snap["delivery_id"] == 4 and snap["remaining_m"] > 0
    asyncio.run(go())


def test_remove_busy_robot_refused(planner):
    async def go():
        f, _ = make_fleet(planner, n=2)
        a, b = (12.975, 77.595), (12.962, 77.583)
        robot, legs = f.plan_delivery(planner, a, b, "alt", "fastest", "R001")
        await f.assign(robot, 5, a, b, legs, "alt", "fastest")
        with pytest.raises(MissionError):
            f.remove("R001")
        f.remove("R002")
        assert list(f.robots) == ["R001"]
    asyncio.run(go())
