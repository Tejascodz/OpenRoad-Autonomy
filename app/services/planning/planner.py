"""High-level route planner facade used by the API and the robot controller."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx

from .costs import CostModel, PROFILES
from .dstar_lite import DStarLite
from .geo import haversine
from .graph import nearest_node, node_latlon, path_edges
from .search import Landmarks, NoRouteError, SearchResult, astar, haversine_heuristic
from .trajectory import Trajectory, build_trajectory

ALGORITHMS = ("alt", "astar", "dijkstra", "dstar_lite")


@dataclass
class VehicleLimits:
    max_kmh: float = 25.0
    consumption_kwh_per_km: float = 0.08
    a_lat: float = 1.5
    a_acc: float = 1.0
    a_dec: float = 2.0


@dataclass
class RoutePlan:
    nodes: List[Any]
    trajectory: Trajectory
    distance_m: float
    eta_s: float
    energy_kwh: float
    cost: float
    profile: str
    algorithm: str
    expanded: int
    compute_ms: float
    alternatives: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        return {
            "distance_m": round(self.distance_m, 1),
            "eta_min": round(self.eta_s / 60.0, 2),
            "energy_kwh": round(self.energy_kwh, 4),
            "profile": self.profile,
            "algorithm": self.algorithm,
            "nodes_expanded": self.expanded,
            "compute_ms": round(self.compute_ms, 2),
            "max_curvature": round(float(self.trajectory.curvature.max()) if len(self.trajectory.s) else 0.0, 4),
        }


class RoutePlanner:
    def __init__(self, G: nx.DiGraph, limits: VehicleLimits):
        self.G = G
        self.limits = limits
        self._landmarks: Dict[str, Landmarks] = {}
        self._lock = threading.Lock()

    # ---- helpers ------------------------------------------------------------
    def model(self, profile: str) -> CostModel:
        return CostModel(profile, self.limits.max_kmh, self.limits.consumption_kwh_per_km)

    def snap(self, lat: float, lon: float) -> Tuple[Any, float]:
        return nearest_node(self.G, lat, lon)

    def landmarks(self, profile: str) -> Landmarks:
        with self._lock:
            lm = self._landmarks.get(profile)
            if lm is None:
                lm = Landmarks(self.G, self.model(profile), k=8)
                self._landmarks[profile] = lm
            return lm

    def heuristic_fn(self, target: Any, model: CostModel, algorithm: str):
        if algorithm == "dijkstra":
            return None
        if algorithm == "alt":
            return self.landmarks(model.profile).heuristic(target)
        return haversine_heuristic(self.G, target, model)

    def pair_heuristic(self, model: CostModel):
        k = model.lower_bound_per_m * 0.999
        G = self.G
        return lambda a, b: haversine(node_latlon(G, a), node_latlon(G, b)) * k

    def path_metrics(self, path: List[Any]) -> Tuple[float, float]:
        e_model = self.model("energy")
        dist = energy = 0.0
        for u, v in path_edges(path):
            d = self.G[u][v]
            dist += d["length"]
            energy += e_model.base_cost(u, v, d)
        return dist, energy

    # ---- planning -----------------------------------------------------------
    def search(self, source: Any, target: Any, algorithm: str = "alt", profile: str = "fastest",
               model: Optional[CostModel] = None) -> SearchResult:
        if algorithm not in ALGORITHMS:
            raise ValueError(f"unknown algorithm {algorithm!r}")
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}")
        model = model or self.model(profile)
        if source == target:
            return SearchResult([source], 0.0, 0, 0.0, algorithm)
        if algorithm == "dstar_lite":
            ds = DStarLite(self.G, source, target, model, self.pair_heuristic(model))
            path, exp, ms = ds.plan()
            return SearchResult(path, ds.path_cost(), exp, ms, algorithm, {"dstar": ds})
        h = self.heuristic_fn(target, model, algorithm)
        return astar(self.G, source, target, model, h, algorithm)

    def plan(self, source: Any, target: Any, algorithm: str = "alt", profile: str = "fastest",
             alternatives: int = 0, model: Optional[CostModel] = None) -> RoutePlan:
        res = self.search(source, target, algorithm, profile, model)
        if len(res.path) < 2:
            raise NoRouteError("start and destination snap to the same road node")
        plan = self._to_plan(res, profile)
        plan.extra = res.extra
        if alternatives > 0:
            plan.alternatives = self.alternatives(source, target, profile, res, alternatives)
        return plan

    def _to_plan(self, res: SearchResult, profile: str, start_point=None) -> RoutePlan:
        L = self.limits
        traj = build_trajectory(self.G, res.path, a_lat=L.a_lat, a_acc=L.a_acc, a_dec=L.a_dec,
                                start_point=start_point)
        dist, energy = self.path_metrics(res.path)
        return RoutePlan(res.path, traj, dist, traj.eta_s(L.a_acc), energy, res.cost, profile,
                         res.algorithm, res.expanded, res.time_ms)

    def alternatives(self, source: Any, target: Any, profile: str, best: SearchResult,
                     k: int = 2, max_stretch: float = 1.35, max_overlap: float = 0.6) -> List[Dict[str, Any]]:
        """Penalty method: repeatedly inflate the cost of already-used edges and re-search.

        Accept a candidate if it is at most `max_stretch` x the best cost and shares at most
        `max_overlap` of its length with every accepted route.
        """
        t0 = time.perf_counter()
        base = self.model(profile)
        model = base.copy()
        accepted: List[List[Any]] = [best.path]
        out: List[Dict[str, Any]] = []
        for _ in range(k * 4):
            if len(out) >= k:
                break
            for p in accepted:
                for e in path_edges(p):
                    model.penalties[e] = model.penalties.get(e, 1.0) * 1.4
            try:
                cand = astar(self.G, source, target, model, self.heuristic_fn(target, base, "astar"))
            except NoRouteError:
                break
            real = sum(base.base_cost(u, v, self.G[u][v]) for u, v in path_edges(cand.path))
            if real > best.cost * max_stretch:
                continue
            clen = sum(self.G[u][v]["length"] for u, v in path_edges(cand.path)) or 1.0
            ok = True
            for p in accepted:
                shared = set(path_edges(p)) & set(path_edges(cand.path))
                if sum(self.G[u][v]["length"] for u, v in shared) / clen > max_overlap:
                    ok = False
                    break
            if not ok:
                continue
            accepted.append(cand.path)
            dist, energy = self.path_metrics(cand.path)
            coords = [node_latlon(self.G, n) for n in cand.path]
            out.append({"distance_m": round(dist, 1), "energy_kwh": round(energy, 4),
                        "cost_ratio": round(real / best.cost, 3) if best.cost else 1.0,
                        "coords": [[round(a, 6), round(b, 6)] for a, b in coords]})
        if out:
            out[0]["compute_ms_total"] = round((time.perf_counter() - t0) * 1000, 1)
        return out
